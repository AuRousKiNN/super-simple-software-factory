from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_tickets import repo

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ADWS = ROOT / ".agents/skills/sssf/templates/adws"
sys.path.insert(0, str(TEMPLATE_ADWS))

adw_simple_sdlc = __import__("adw-simple-sdlc")
from adw_modules import agents, changes  # noqa: E402
from adw_modules.agent_codex import (  # noqa: E402
    SDK_VERSION,
    CodexRuntime,
    RuntimeHooks,
    _runtime_version,
)
from adw_modules.codex_events import SubagentTracker  # noqa: E402
from adw_modules.data_types import (  # noqa: E402
    AgentRunRequest,
    BaseRef,
    BuildOutput,
    ChangeSet,
    ChangesOutput,
    DocumentOutput,
    PlanOutput,
    QualityResult,
    QualityCheckResult,
    ReviewOutput,
    SSSFConfig,
    SubagentConfig,
)


ROLE_TEMPLATE = (
    ROOT / ".agents/skills/sssf/templates/codex_agents/sssf_recon.toml"
)


def _config(tmp_path: Path, *, owner: str = "planner") -> SSSFConfig:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("role\n\n{{subagent_instructions}}")
    user.write_text("{{prompt}}")
    return SSSFConfig.model_validate({
        "schema_version": 2,
        "defaults": {
            "coding_agent": "codex",
            "model": "gpt-5.6-terra",
            "thinking": "low",
            "subagents": {"enabled": False},
        },
        "codex": {"turn_timeout_s": 10, "shutdown_grace_s": 1},
        "agents": [{
            "name": owner,
            "subagents": {
                "enabled": True,
                "max_concurrent": 2,
                "role": "sssf_recon",
                "config_file": ".codex/agents/sssf_recon.toml",
            },
            "prompt_engineering": {"system": str(system), "user": str(user)},
        }],
    })


def _install_role(tmp_path: Path) -> Path:
    role = tmp_path / ".codex/agents/sssf_recon.toml"
    role.parent.mkdir(parents=True, exist_ok=True)
    role.write_text(ROLE_TEMPLATE.read_text().replace(
        "{{sssf_skill_path_toml}}",
        json.dumps(str(ROOT / ".agents/skills/sssf/SKILL.md")),
    ))
    return role


def test_config_enables_recon_parents_and_validates_child_role(
    tmp_path: Path, monkeypatch,
) -> None:
    template = agents.load_config(
        str(ROOT / ".agents/skills/sssf/templates/sssf.config.yaml")
    )
    assert {agent.name for agent in template.agents if agent.subagents.enabled} == {
        "planner", "scout", "decomposer",
    }
    assert all(agent.subagents.max_concurrent <= 6 for agent in template.agents)

    _install_role(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path)
    agents.validate(cfg, ["planner"])

    role = tmp_path / ".codex/agents/sssf_recon.toml"
    role.write_text(role.read_text().replace("enabled = false", "enabled = true", 1))
    with pytest.raises(SystemExit, match="agents.enabled=false"):
        agents.validate(cfg, ["planner"])

    _install_role(tmp_path)
    builder = _config(tmp_path, owner="builder")
    with pytest.raises(SystemExit, match="only planner/scout"):
        agents.validate(builder, ["builder"])


@dataclass
class _Notification:
    method: str
    payload: dict


class _Handle:
    def __init__(self, events: list[_Notification]) -> None:
        self.id = "turn-parent"
        self.events = events
        self.interrupted = False

    def stream(self):
        yield from self.events

    def interrupt(self):
        self.interrupted = True


class _Thread:
    id = "thread-parent"

    def __init__(self, events: list[_Notification]) -> None:
        self.events = events

    def turn(self, _prompt, **_kwargs):
        return _Handle(self.events)


class _Codex:
    def __init__(self, events: list[_Notification]) -> None:
        self.metadata = SimpleNamespace(serverInfo=SimpleNamespace(
            version=f"{SDK_VERSION} (synthetic-platform) sdk-transport",
        ))
        self.thread = _Thread(events)
        self.thread_start_kwargs = None
        self.closed = False

    def models(self):
        effort = SimpleNamespace(reasoning_effort="low")
        model = SimpleNamespace(
            id="gpt-5.6-terra", supported_reasoning_efforts=[effort],
        )
        return SimpleNamespace(data=[model])

    def thread_start(self, **kwargs):
        self.thread_start_kwargs = kwargs
        return self.thread

    def thread_resume(self, _thread_id, **kwargs):
        self.thread_start_kwargs = kwargs
        return self.thread

    def close(self):
        self.closed = True


def _activity(kind: str, label: str = "alpha") -> _Notification:
    return _Notification("item/started", {
        "threadId": "thread-parent",
        "turnId": "turn-parent",
        "item": {
            "id": f"activity-{kind}",
            "type": "subAgentActivity",
            "agentPath": f"/root/{label}",
            "agentThreadId": "thread-child",
            "kind": kind,
        },
    })


def _parent_events(*, label: str = "alpha", finish_child: bool = True):
    events = [
        _Notification("turn/started", {
            "threadId": "thread-parent", "turn": {"id": "turn-parent"},
        }),
        _Notification("item/completed", {
            "threadId": "thread-parent",
            "turnId": "turn-parent",
            "item": {
                "id": "spawn-1",
                "type": "collabAgentToolCall",
                "tool": "spawnAgent",
                "status": "completed",
                "senderThreadId": "thread-parent",
                "receiverThreadIds": ["thread-child"],
                "agentsStates": {},
                "prompt": "inspect module A",
                "model": None,
                "reasoningEffort": None,
            },
        }),
        _activity("started", label),
    ]
    if finish_child:
        events.extend([
            _Notification("thread/tokenUsage/updated", {
                "threadId": "thread-child",
                "turnId": "turn-child",
                "tokenUsage": {"last": {
                    "inputTokens": 4, "cachedInputTokens": 1,
                    "outputTokens": 2, "reasoningOutputTokens": 1,
                    "totalTokens": 6,
                }},
            }),
            _activity("completed", label),
            _Notification("item/completed", {
                "threadId": "thread-parent",
                "turnId": "turn-parent",
                "item": {
                    "id": "wait-1",
                    "type": "collabAgentToolCall",
                    "tool": "wait",
                    "status": "completed",
                    "senderThreadId": "thread-parent",
                    "receiverThreadIds": ["thread-child"],
                    "agentsStates": {
                        "thread-child": {"status": "completed", "message": "child-ok"},
                    },
                    "prompt": None,
                    "model": None,
                    "reasoningEffort": None,
                },
            }),
        ])
    events.extend([
        _Notification("item/completed", {
            "threadId": "thread-parent",
            "turnId": "turn-parent",
            "item": {
                "id": "message-parent", "type": "agentMessage",
                "phase": "final_answer", "text": '{"status":"success"}',
            },
        }),
        _Notification("thread/tokenUsage/updated", {
            "threadId": "thread-parent",
            "turnId": "turn-parent",
            "tokenUsage": {"last": {
                "inputTokens": 10, "cachedInputTokens": 4,
                "outputTokens": 3, "reasoningOutputTokens": 1,
                "totalTokens": 13,
            }},
        }),
        _Notification("turn/completed", {
            "threadId": "thread-parent",
            "turn": {"id": "turn-parent", "status": "completed", "error": None},
        }),
    ])
    return events


def _request(tmp_path: Path) -> AgentRunRequest:
    return AgentRunRequest(
        role="planner",
        cwd=str(tmp_path),
        model="gpt-5.6-terra",
        effort="low",
        developer_instructions="planner",
        prompt="plan",
        output_schema={"type": "object", "properties": {}},
        raw_output_path=str(tmp_path / "raw.jsonl"),
        subagents=SubagentConfig(
            enabled=True, max_concurrent=2, role="sssf_recon",
            config_file=".codex/agents/sssf_recon.toml",
        ),
    )


def test_runtime_enforces_child_policy_records_results_and_does_not_double_count_usage(
    tmp_path: Path,
) -> None:
    client = _Codex(_parent_events())
    runtime = CodexRuntime(
        _config(tmp_path).codex,
        tmp_path,
        codex_factory=lambda _launch: client,
        version_provider=lambda _name: SDK_VERSION,
    )
    result = runtime.run_turn(_request(tmp_path), RuntimeHooks())
    assert result.status == "completed"
    assert result.usage.total_tokens == 13
    assert result.child_usage_attribution == "separate"
    assert len(result.subagents) == 1
    child = result.subagents[0]
    assert child.parent_thread_id == "thread-parent"
    assert child.parent_turn_id == "turn-parent"
    assert child.role == "sssf_recon"
    assert child.agent_path == "/root/alpha"
    assert child.task == "inspect module A"
    assert child.status == "completed"
    assert child.result == "child-ok"
    assert child.usage is not None and child.usage.total_tokens == 6
    assert client.thread_start_kwargs["config"]["agents"] == {
        "enabled": True,
        "max_concurrent_threads_per_session": 2,
        "interrupt_message": True,
    }
    runtime.close()


def test_parent_cannot_complete_with_disabled_or_unsettled_child(tmp_path: Path) -> None:
    client = _Codex(_parent_events(finish_child=False))
    runtime = CodexRuntime(
        _config(tmp_path).codex,
        tmp_path,
        codex_factory=lambda _launch: client,
        version_provider=lambda _name: SDK_VERSION,
    )
    request = _request(tmp_path).model_copy(update={
        "subagents": SubagentConfig(enabled=False),
    })
    result = runtime.run_turn(request, RuntimeHooks())
    assert result.status == "failed"
    assert result.error is not None and result.error.kind == "subagent_policy"
    assert "spawned while subagents are disabled" in result.error.message
    assert "before child threads settled" in result.error.message
    assert result.subagent_cleanup_forced is True
    assert client.closed is True


def test_transport_failure_with_active_child_forces_runtime_cleanup(tmp_path: Path) -> None:
    class ExplodingHandle(_Handle):
        def stream(self):
            yield from self.events
            raise RuntimeError("synthetic transport failure")

    class ExplodingThread(_Thread):
        def turn(self, _prompt, **_kwargs):
            return ExplodingHandle(self.events)

    events = _parent_events(finish_child=False)[:3]
    client = _Codex([])
    client.thread = ExplodingThread(events)
    runtime = CodexRuntime(
        _config(tmp_path).codex,
        tmp_path,
        codex_factory=lambda _launch: client,
        version_provider=lambda _name: SDK_VERSION,
    )

    result = runtime.run_turn(_request(tmp_path), RuntimeHooks())

    assert result.status == "failed"
    assert result.error is not None and result.error.kind == "runtime"
    assert result.subagent_cleanup_forced is True
    assert client.closed is True


def test_runtime_version_only_accepts_decorated_exact_semver() -> None:
    exact = SimpleNamespace(serverInfo=SimpleNamespace(
        version=f"{SDK_VERSION} (synthetic-platform)",
    ))
    prerelease = SimpleNamespace(serverInfo=SimpleNamespace(
        version=f"{SDK_VERSION}-dev",
    ))

    assert _runtime_version(exact) == SDK_VERSION
    assert _runtime_version(prerelease) == f"{SDK_VERSION}-dev"


def test_subagent_tracker_rejects_concurrency_above_policy() -> None:
    tracker = SubagentTracker(SubagentConfig(enabled=True, max_concurrent=1))
    first = {
        "method": "item/started",
        "params": {
            "threadId": "thread-parent",
            "turnId": "turn-parent",
            "item": {
                "type": "subAgentActivity",
                "agentThreadId": "thread-child-a",
                "agentPath": "/root/a",
                "kind": "started",
            },
        },
    }
    second = {
        "method": "item/started",
        "params": {
            "threadId": "thread-parent",
            "turnId": "turn-parent",
            "item": {
                "type": "subAgentActivity",
                "agentThreadId": "thread-child-b",
                "agentPath": "/root/b",
                "kind": "started",
            },
        },
    }

    tracker.observe(first)
    tracker.observe(second)

    assert tracker.peak_concurrent == 2
    assert tracker.violations == [
        "subagent concurrency 2 exceeded configured maximum 1",
    ]


class _PhaseHandle:
    def __init__(self, run, params) -> None:
        self.run = run
        self.params = params

    def log(self, **_payload):
        pass

    def call(self, call):
        self.run.calls.append((self.params.name, call))
        if self.params.owner == "planner":
            return PlanOutput(
                status="success", summary="planned", artifacts=["plan.md", "spec.md"],
                commit_message="添加计划", spec_path="spec.md",
            )
        if self.params.owner == "builder":
            return BuildOutput(
                status="success", summary="built", commit_message="实现功能",
            )
        if self.params.owner == "reviewer":
            return ReviewOutput(
                status="success", summary="approved", approved=True,
                artifacts=["review.md"], findings=[dict(requirement="REQ-01", met=True, evidence="src.py")],
            )
        if self.params.owner == "documenter":
            return DocumentOutput(
                status="success", summary="documented", spec_path="spec.md", overview_path="README.md", document_path="document.md",
                documented_files=["src.py"], artifacts=["document.md", "app_docs/x.md"],
                commit_message="补充文档",
            )
        raise AssertionError(self.params.owner)


class _Run:
    def __init__(self) -> None:
        self.adw_id = "m3-chain"
        self.engineer = "tester"
        self.phases = []
        self.calls = []
        self.accepted = None

    @contextmanager
    def phase(self, params):
        self.phases.append(params)
        yield _PhaseHandle(self, params)

    def finish(self, *, accepted, reason):
        self.accepted = (accepted, reason)
        return 0 if accepted else 1


def test_complete_sdlc_chain_reaches_documentation(monkeypatch, repo) -> None:
    from test_review_routing import setup_flow, review
    from adw_modules.data_types import PlanningTarget
    run = setup_flow(monkeypatch, repo, [review()], adw_simple_sdlc)
    assert adw_simple_sdlc.main("build it", target=PlanningTarget(spec_dir="specs/example")) == 0
    assert [p.owner for p, c in run.calls] == ["planner", "builder", "reviewer", "documenter"]
    review_call = next(c for p, c in run.calls if p.owner == "reviewer")
    assert isinstance(review_call.previous, ChangesOutput)
    assert review_call.work_item.spec.path == "specs/example/spec.md"
    assert run.accepted is True


def test_change_capture_includes_untracked_new_files(tmp_path: Path, monkeypatch) -> None:
    base = BaseRef(ref="a" * 40, commit="a" * 40, reason="pinned")
    monkeypatch.setattr(changes, "resolve_base", lambda _ref: base)
    monkeypatch.setattr(changes.git_helper, "diff_files", lambda _base: ["edited.py", "deleted.py"])
    monkeypatch.setattr(changes.git_helper, "untracked_files", lambda: ["new.py"])
    monkeypatch.setattr(changes.git_helper, "diff_counts", lambda _base: (2, 1))
    monkeypatch.setattr(changes.git_helper, "diff_stat", lambda _base: "2 files changed")
    monkeypatch.setattr(changes.git_helper, "diff_text", lambda _base: "diff --git a/edited.py")
    monkeypatch.setattr(changes.git_helper, "short_sha", lambda value: value[:7])

    run = SimpleNamespace(context_handoff_dir=tmp_path)
    captured = changes.capture(run, changes.ChangeCapture(base=base.ref))
    envelope = changes.as_envelope(captured)

    assert captured.files == ["edited.py", "deleted.py"]
    assert captured.untracked == ["new.py"]
    assert envelope.changed_files == ["edited.py", "deleted.py", "new.py"]
    assert "new.py" in Path(captured.diff_path).read_text()
