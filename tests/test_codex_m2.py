from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ADWS = ROOT / ".claude/skills/sssf/templates/adws"
sys.path.insert(0, str(TEMPLATE_ADWS))

from adw_modules import agents, permissions  # noqa: E402
from adw_modules.agent_codex import CodexRuntime, SDK_VERSION  # noqa: E402
from adw_modules.codex_events import CodexEventCollector, ToolCallTracker  # noqa: E402
from adw_modules.data_types import (  # noqa: E402
    AgentCall,
    AgentRunResult,
    GenericOutput,
    Phase,
    PhaseParams,
    RuntimeErrorInfo,
    SSSFConfig,
    UsageBreakdown,
)
from adw_modules.tracer import Tracer  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "m2@example.invalid")
    _git(repo, "config", "user.name", "M2 Test")
    (repo / ".gitignore").write_text(".runtime/\n")
    (repo / "same.txt").write_text("aaaa\n")
    (repo / "system.md").write_text("role")
    (repo / "user.md").write_text("{{prompt}}")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _config(repo: Path, *, writes: list[str] | None = None) -> SSSFConfig:
    return SSSFConfig.model_validate({
        "schema_version": 2,
        "defaults": {
            "coding_agent": "codex",
            "model": "gpt-5.6-terra",
            "thinking": "low",
            "subagents": {"enabled": False},
            "data_dir": ".runtime",
        },
        "agents": [{
            "name": "builder",
            "writes": writes,
            "prompt_engineering": {
                "system": str(repo / "system.md"),
                "user": str(repo / "user.md"),
            },
        }],
    })


def test_content_snapshot_restores_dirty_untracked_mode_symlink_and_index(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    tracked = repo / "same.txt"
    tracked.write_text("bbbb\n")  # pre-existing dirty content, same line count
    tracked.chmod(0o640)
    untracked = repo / "notes.tmp"
    untracked.write_text("before")
    link = repo / "link.tmp"
    link.symlink_to("same.txt")

    run = SimpleNamespace(
        repo_root=repo,
        adw_id="m2-permissions",
        cfg=_config(repo, writes=[]),
        context_handoff_dir=repo / ".runtime/sessions/m2/context_handoff",
        active_report_dir=None,
        workspace_lock=permissions.acquire_workspace_lock(repo),
    )
    before = permissions.snapshot(run)
    assert not before.backup_dir.is_relative_to(repo)
    try:
        tracked.write_text("cccc\n")
        tracked.chmod(0o600)
        untracked.write_text("after")
        link.unlink()
        link.symlink_to("user.md")
        _git(repo, "add", "same.txt")

        with pytest.raises(permissions.PermissionBreach) as caught:
            permissions.enforce(
                run,
                SimpleNamespace(phase_id="phase"),
                run.cfg.agents[0],
                before,
            )
        message = str(caught.value)
        assert "same.txt" in message
        assert "notes.tmp" in message
        assert "link.tmp" in message
        assert permissions.INDEX_PATH in message
        assert tracked.read_text() == "bbbb\n"
        assert tracked.stat().st_mode & 0o777 == 0o640
        assert untracked.read_text() == "before"
        assert os.readlink(link) == "same.txt"
        assert subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=repo, check=False,
        ).returncode == 0
    finally:
        before.cleanup()
        run.workspace_lock.release()


def test_runtime_data_dir_is_not_a_blanket_write_exception(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    run = SimpleNamespace(
        repo_root=repo,
        cfg=_config(repo, writes=[]),
        context_handoff_dir=repo / ".runtime/sessions/current/context_handoff",
        active_report_dir=repo / ".runtime/sessions/current/builder/inv/reports",
    )
    agent = run.cfg.agents[0]
    assert permissions.permitted(
        ".runtime/sessions/current/context_handoff/report.md", agent, run.cfg, run,
    )
    assert not permissions.permitted(
        ".runtime/prompt_engineering/builder/system.md", agent, run.cfg, run,
    )
    with pytest.raises(ValueError):
        permissions.permitted("../outside", agent, run.cfg, run)


class _Trace:
    def __init__(self) -> None:
        self.events = []
        self.envelopes = []

    def event(self, record):
        self.events.append(record)

    def gate_row(self, *_args):
        pass

    def envelope_row(self, *args):
        self.envelopes.append(args)

    def agent_session_row(self, *_args, **_kwargs):
        pass


class _Console:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _MutatingFailedRuntime:
    def __init__(self, target: Path) -> None:
        self.target = target

    def run_turn(self, request, hooks):
        hooks.on_thread_ready("thread-m2", "0.155.1", False)
        hooks.on_turn_started("turn-m2")
        self.target.write_text("agent overwrote dirty work\n")
        result = AgentRunResult(
            thread_id="thread-m2",
            turn_id="turn-m2",
            status="failed",
            error=RuntimeErrorInfo(kind="runtime", message="crash"),
            usage=UsageBreakdown(input_tokens=2, output_tokens=1, total_tokens=3),
            runtime_version="0.155.1",
        )
        hooks.on_turn_finished(result)
        return result


def test_failed_turn_still_enforces_and_records_agent_end(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = repo / "same.txt"
    target.write_text("engineer dirty state\n")
    cfg = _config(repo, writes=[])
    session_dir = repo / ".runtime/sessions/adw-m2"
    session_dir.mkdir(parents=True)
    run = SimpleNamespace(
        cfg=cfg,
        adw_id="adw-m2",
        repo_root=repo,
        session_dir=session_dir,
        context_handoff_dir=session_dir / "context_handoff",
        runtime=_MutatingFailedRuntime(target),
        tracer=_Trace(),
        console=_Console(),
        agent_map={"schema_version": 2, "agents": {}},
        tokens=0,
        active_report_dir=None,
        workspace_lock=permissions.acquire_workspace_lock(repo),
    )
    run.context_handoff_dir.mkdir()
    run.agent_entry = lambda name: run.agent_map["agents"].get(name)
    run.save_agent_map = lambda name, entry: run.agent_map["agents"].update({name: entry})
    run.add_usage = lambda usage, **_kwargs: setattr(run, "tokens", run.tokens + usage.total_tokens)
    phase = Phase(
        phase_id="phase-m2", adw_id=run.adw_id, seq=1,
        params=PhaseParams(
            name="build", kind="agent", owner="builder", description="fail safely",
        ),
    )
    try:
        with pytest.raises(permissions.PermissionBreach):
            agents.execute(
                run, phase, AgentCall(output_type=GenericOutput, prompt="mutate then fail"),
            )
    finally:
        run.workspace_lock.release()
    assert target.read_text() == "engineer dirty state\n"
    assert [event.type for event in run.tracer.events].count("agent_end") == 1
    assert any(event.name == "permission_breach" for event in run.tracer.events)


def _notification(method: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(method=method, payload=payload)


def test_usage_delta_unknown_events_and_tool_deduplication(tmp_path: Path) -> None:
    baseline = UsageBreakdown(
        input_tokens=10, cached_input_tokens=4, uncached_input_tokens=6,
        output_tokens=2, reasoning_tokens=1, total_tokens=12,
    )
    collector = CodexEventCollector(tmp_path / "raw.jsonl", baseline)
    collector.observe(_notification("future/notification", {"value": 1}))
    collector.observe(_notification("thread/tokenUsage/updated", {
        "tokenUsage": {
            "total": {
                "inputTokens": 18,
                "cachedInputTokens": 9,
                "outputTokens": 5,
                "reasoningOutputTokens": 2,
                "totalTokens": 23,
            },
            "modelContextWindow": 200,
        },
    }))
    assert collector.unknown_event_count == 1
    assert collector.usage.input_tokens == 8
    assert collector.usage.cached_input_tokens == 5
    assert collector.usage.total_tokens == 11
    assert collector.context_tokens is None
    assert collector.context_window == 200

    tracker = ToolCallTracker("invocation-1")
    started = {
        "method": "item/started",
        "received_at_ns": 1_000_000_000,
        "params": {
            "threadId": "thread", "turnId": "turn",
            "item": {
                "id": "item", "type": "commandExecution",
                "command": "echo ok", "status": "inProgress",
            },
        },
    }
    completed = {
        "method": "item/completed",
        "received_at_ns": 2_000_000_000,
        "params": {
            "threadId": "thread", "turnId": "turn",
            "item": {
                "id": "item", "type": "commandExecution", "command": "echo ok",
                "status": "completed", "exitCode": 0, "aggregatedOutput": "ok",
            },
        },
    }
    assert tracker.observe(started) == []
    rows = tracker.observe(completed)
    assert len(rows) == 1
    assert rows[0]["started_at"] is not None
    assert rows[0]["invocation_id"] == "invocation-1"
    assert tracker.observe(completed) == []


def test_schema_v2_rejects_old_db_and_settles_turn_once(tmp_path: Path) -> None:
    old = tmp_path / "old.db"
    connection = sqlite3.connect(old)
    connection.execute("CREATE TABLE sessions (adw_id TEXT PRIMARY KEY)")
    connection.close()
    with pytest.raises(RuntimeError, match="missing schema_meta"):
        Tracer(old, tmp_path / "old.jsonl")

    tracer = Tracer(tmp_path / "sssf.db", tmp_path / "events.jsonl")
    tracer.session_start("adw", "engineer")
    phase = Phase(
        phase_id="phase", adw_id="adw", seq=1,
        params=PhaseParams(
            name="build", kind="agent", owner="builder", description="settle once",
        ),
    )
    tracer.phase_upsert(phase)
    tracer.agent_invocation_start("inv", phase, "builder", "0.155.1")
    usage = UsageBreakdown(input_tokens=10, output_tokens=3, total_tokens=13)
    result = AgentRunResult(
        thread_id="thread", turn_id="turn", status="failed", usage=usage,
        runtime_version="0.155.1",
    )
    assert tracer.agent_invocation_finish("inv", "adw", result, usage) is True
    assert tracer.agent_invocation_finish("inv", "adw", result, usage) is False
    row = tracer.conn.execute(
        "SELECT total_tokens, total_cost, cost_complete FROM sessions WHERE adw_id='adw'"
    ).fetchone()
    assert row == (13, 0.0, 0)
    invocation = tracer.conn.execute(
        "SELECT status, usage_settled FROM agent_invocations WHERE invocation_id='inv'"
    ).fetchone()
    assert invocation == ("failed", 1)


def test_runtime_close_always_runs_owned_process_cleanup(tmp_path: Path) -> None:
    class Supervisor:
        def __init__(self) -> None:
            self.terminations: list[int] = []

        def direct_children(self):
            return {}

        def claim_new(self, _before):
            pass

        def terminate_all(self, grace_s):
            self.terminations.append(grace_s)

    class Client:
        metadata = SimpleNamespace(
            serverInfo=SimpleNamespace(version=SDK_VERSION),
        )

        def models(self):
            model = SimpleNamespace(
                id="gpt-5.6-terra",
                supported_reasoning_efforts=[SimpleNamespace(reasoning_effort="low")],
            )
            return SimpleNamespace(data=[model])

        def close(self):
            pass

    supervisor = Supervisor()
    runtime = CodexRuntime(
        _config(_repo(tmp_path)).codex,
        tmp_path,
        codex_factory=lambda _launch: Client(),
        version_provider=lambda _name: SDK_VERSION,
        process_supervisor=supervisor,
    )
    runtime.preflight("gpt-5.6-terra", "low")
    runtime.close()
    assert supervisor.terminations == [runtime.config.shutdown_grace_s]
