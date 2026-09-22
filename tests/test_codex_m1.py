from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field, ValidationError

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ADWS = ROOT / ".agents/skills/sssf/templates/adws"
sys.path.insert(0, str(TEMPLATE_ADWS))

from adw_modules import agents  # noqa: E402
from adw_modules.agent_codex import CodexRuntime, RuntimeHooks, SDK_VERSION  # noqa: E402
from adw_modules.codex_schema import (  # noqa: E402
    UnsupportedOutputSchema,
    strict_output_schema,
)
from adw_modules.data_types import (  # noqa: E402
    AgentCall,
    AgentRunRequest,
    AgentRunResult,
    BuildOutput,
    ChangesOutput,
    DocumentOutput,
    GenericOutput,
    Phase,
    PhaseParams,
    PlanOutput,
    ReviewOutput,
    RuntimeErrorInfo,
    ScoutOutput,
    SSSFConfig,
    UsageBreakdown,
    VerifyOutput,
)
from adw_modules.runner import Run  # noqa: E402


def _config(system: Path, user: Path) -> SSSFConfig:
    return SSSFConfig.model_validate({
        "schema_version": 2,
        "defaults": {
            "coding_agent": "codex",
            "model": "gpt-5.6-terra",
            "thinking": "low",
            "subagents": {"enabled": False},
        },
        "codex": {"turn_timeout_s": 10},
        "agents": [{
            "name": "builder",
            "prompt_engineering": {"system": str(system), "user": str(user)},
        }],
    })


def test_config_v2_merges_known_fields_and_rejects_legacy_keys(tmp_path: Path) -> None:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("role")
    user.write_text("{{prompt}}")
    config = tmp_path / "sssf.yaml"
    config.write_text(
        """schema_version: 2
defaults:
  coding_agent: codex
  model: gpt-5.6-terra
  thinking: low
  writes: []
  subagents:
    enabled: false
codex:
  auth: cli
agents:
  - name: builder
    writes: null
    prompt_engineering:
      system: SYSTEM
      user: USER
""".replace("SYSTEM", str(system)).replace("USER", str(user))
    )

    loaded = agents.load_config(str(config))
    assert loaded.schema_version == 2
    assert loaded.agents[0].model == "gpt-5.6-terra"
    assert loaded.agents[0].writes is None
    assert loaded.agents[0].subagents.enabled is False

    legacy = config.read_text().replace("coding_agent: codex", "coding_agent: pi", 1)
    config.write_text(legacy)
    with pytest.raises(ValidationError):
        agents.load_config(str(config))

    template = agents.load_config(
        str(ROOT / ".agents/skills/sssf/templates/sssf.config.yaml")
    )
    assert template.schema_version == 2
    assert {agent.coding_agent for agent in template.agents} == {"codex"}
    enabled = {agent.name for agent in template.agents if agent.subagents.enabled}
    assert enabled == {"planner", "scout", "decomposer"}

    config.write_text(legacy.replace("coding_agent: pi", "coding_agent: codex", 1)
                      + "unknown_root: true\n")
    with pytest.raises(ValidationError):
        agents.load_config(str(config))


def test_strict_schema_closes_nested_objects_and_rejects_unverified_keywords() -> None:
    for output_type in (
        GenericOutput,
        PlanOutput,
        BuildOutput,
        ScoutOutput,
        ReviewOutput,
        DocumentOutput,
        ChangesOutput,
        VerifyOutput,
    ):
        schema = strict_output_schema(output_type)
        assert schema["additionalProperties"] is False
        assert schema["required"] == list(schema["properties"])
        assert "default" not in json.dumps(schema)

    class PatternOutput(BaseModel):
        value: str = Field(pattern="^safe$")

    with pytest.raises(UnsupportedOutputSchema, match="pattern"):
        strict_output_schema(PatternOutput)


def test_builder_does_not_claim_changed_files() -> None:
    with pytest.raises(ValidationError, match="changed_files"):
        BuildOutput(status="success", changed_files=["src.py"])


@dataclass
class _Notification:
    method: str
    payload: dict


class _FakeHandle:
    def __init__(self, turn_id: str, events: list[_Notification]) -> None:
        self.id = turn_id
        self._events = events
        self.interrupted = False

    def stream(self):
        yield from self._events

    def interrupt(self):
        self.interrupted = True


class _FakeThread:
    def __init__(self, thread_id: str, event_batches: list[list[_Notification]]) -> None:
        self.id = thread_id
        self._event_batches = event_batches
        self.turn_requests = []

    def turn(self, prompt, **kwargs):
        self.turn_requests.append((prompt, kwargs))
        return _FakeHandle(f"turn-{len(self.turn_requests)}", self._event_batches.pop(0))


class _FakeCodex:
    def __init__(self, _launch, event_batches: list[list[_Notification]]) -> None:
        self.metadata = SimpleNamespace(serverInfo=SimpleNamespace(version=SDK_VERSION))
        self._thread = _FakeThread("thread-1", event_batches)
        self.closed = False

    def models(self):
        option = SimpleNamespace(reasoning_effort="low")
        model = SimpleNamespace(
            id="gpt-5.6-terra", supported_reasoning_efforts=[option],
        )
        return SimpleNamespace(data=[model])

    def thread_start(self, **_kwargs):
        return self._thread

    def thread_resume(self, thread_id, **_kwargs):
        assert thread_id == self._thread.id
        return self._thread

    def close(self):
        self.closed = True


def _completed_events(text: str, *, status: str = "completed") -> list[_Notification]:
    error = None if status == "completed" else {"message": "model refused the request"}
    return [
        _Notification("turn/started", {"turn": {"id": "turn-1"}}),
        _Notification("item/completed", {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "item": {
                "id": "message-1",
                "type": "agentMessage",
                "phase": "final_answer",
                "text": text,
            },
        }),
        _Notification("thread/tokenUsage/updated", {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "tokenUsage": {
                "last": {
                    "inputTokens": 10,
                    "cachedInputTokens": 4,
                    "outputTokens": 3,
                    "reasoningOutputTokens": 1,
                    "totalTokens": 13,
                },
                "total": {
                    "inputTokens": 30,
                    "cachedInputTokens": 10,
                    "outputTokens": 12,
                    "reasoningOutputTokens": 2,
                    "totalTokens": 42,
                },
                "modelContextWindow": 100,
            },
        }),
        _Notification("turn/completed", {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "status": status, "error": error},
        }),
    ]


def test_runtime_streams_usage_and_never_accepts_failed_text(tmp_path: Path) -> None:
    batches = [
        _completed_events('{"status":"success"}'),
        _completed_events('{"status":"success"}', status="failed"),
    ]
    created = []

    def factory(launch):
        client = _FakeCodex(launch, batches)
        created.append(client)
        return client

    runtime = CodexRuntime(
        _config(tmp_path / "unused-system", tmp_path / "unused-user").codex,
        tmp_path,
        codex_factory=factory,
        version_provider=lambda _name: SDK_VERSION,
    )
    seen = []
    request = AgentRunRequest(
        role="builder",
        cwd=str(tmp_path),
        model="gpt-5.6-terra",
        effort="low",
        developer_instructions="role",
        prompt="first",
        output_schema={"type": "object", "properties": {}},
        raw_output_path=str(tmp_path / "raw.jsonl"),
    )
    first = runtime.run_turn(
        request,
        RuntimeHooks(on_thread_ready=lambda *args: seen.append(args)),
    )
    assert first.status == "completed"
    assert first.thread_id == "thread-1"
    assert first.usage.total_tokens == 13
    assert first.usage.uncached_input_tokens == 6
    # Thread cumulative usage is billing history, not live context occupancy.
    assert first.context_tokens is None
    assert first.context_window == 100
    assert seen == [("thread-1", SDK_VERSION, False)]

    second = runtime.run_turn(
        request.model_copy(update={"thread_id": "thread-1", "prompt": "second"}),
        RuntimeHooks(),
    )
    assert second.status == "failed"
    assert second.text == '{"status":"success"}'
    assert second.error is not None
    runtime.close()
    assert created[0].closed is True


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


class _QueuedRuntime:
    def __init__(self, texts: list[str], *, fail: bool = False) -> None:
        self.texts = texts
        self.requests = []
        self.fail = fail

    def run_turn(self, request, hooks):
        self.requests.append(request)
        thread_id = request.thread_id or "thread-shared"
        hooks.on_thread_ready(thread_id, SDK_VERSION, bool(request.thread_id))
        turn_id = f"turn-{len(self.requests)}"
        hooks.on_turn_started(turn_id)
        text = self.texts.pop(0)
        result = AgentRunResult(
            thread_id=thread_id,
            turn_id=turn_id,
            text=text,
            status="failed" if self.fail else "completed",
            error=(RuntimeErrorInfo(kind="runtime", message="failed with text")
                   if self.fail else None),
            usage=UsageBreakdown(
                input_tokens=2,
                cached_input_tokens=1,
                uncached_input_tokens=1,
                output_tokens=1,
                total_tokens=3,
            ),
            runtime_version=SDK_VERSION,
        )
        hooks.on_turn_finished(result)
        return result


class _Run:
    def __init__(self, tmp_path: Path, cfg: SSSFConfig, runtime) -> None:
        self.cfg = cfg
        self.adw_id = "adw-test"
        self.repo_root = ROOT
        self.session_dir = tmp_path / "sessions" / self.adw_id
        self.session_dir.mkdir(parents=True)
        self.context_handoff_dir = self.session_dir / "context_handoff"
        self.context_handoff_dir.mkdir()
        self.runtime = runtime
        self.tracer = _Trace()
        self.console = _Console()
        self.agent_map = {"schema_version": 2, "agents": {}}
        self.tokens = 0

    def agent_entry(self, name):
        return self.agent_map["agents"].get(name)

    def save_agent_map(self, name, entry):
        self.agent_map["agents"][name] = dict(entry)

    def add_usage(self, usage):
        self.tokens += usage.total_tokens


def test_json_and_gate_repairs_reuse_one_thread(tmp_path: Path, monkeypatch) -> None:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("builder role")
    user.write_text("{{prompt}}")
    cfg = _config(system, user)
    runtime = _QueuedRuntime([
        "not json",
        '{"status":"success","summary":"first","artifacts":[],"notes_for_next_agent":""}',
        '{"status":"success","summary":"fixed","artifacts":[],"notes_for_next_agent":""}',
    ])
    run = _Run(tmp_path, cfg, runtime)
    phase = Phase(
        phase_id="phase-1",
        adw_id=run.adw_id,
        seq=1,
        params=PhaseParams(
            name="build",
            kind="agent",
            owner="builder",
            description="Build and validate the requested change.",
            retries=1,
        ),
    )
    gate_calls = 0

    def gate(_envelope, _run):
        nonlocal gate_calls
        gate_calls += 1
        return ["summary must be fixed"] if gate_calls == 1 else []

    monkeypatch.setattr("adw_modules.agents.permissions.snapshot", lambda _run: {})
    monkeypatch.setattr(
        "adw_modules.agents.permissions.enforce",
        lambda *_args, **_kwargs: [],
    )
    envelope = agents.execute(
        run,
        phase,
        AgentCall(output_type=GenericOutput, prompt="build it", gates=[gate]),
    )
    assert envelope.summary == "fixed"
    assert [request.thread_id for request in runtime.requests] == [
        None,
        "thread-shared",
        "thread-shared",
    ]
    assert all(request.output_schema for request in runtime.requests)
    assert run.agent_map["agents"]["builder"]["state"] == "idle"
    assert run.tokens == 9

    run.cfg.agents[0].thinking = "high"
    with pytest.raises(RuntimeError, match="saved Codex thread.*incompatible"):
        agents.execute(
            run,
            phase,
            AgentCall(output_type=GenericOutput, prompt="changed config"),
        )
    assert len(runtime.requests) == 3


def test_failed_turn_with_valid_json_is_not_accepted(tmp_path: Path, monkeypatch) -> None:
    system = tmp_path / "system.md"
    user = tmp_path / "user.md"
    system.write_text("builder role")
    user.write_text("{{prompt}}")
    runtime = _QueuedRuntime([
        '{"status":"success","summary":"looks valid","artifacts":[],"notes_for_next_agent":""}',
    ], fail=True)
    run = _Run(tmp_path, _config(system, user), runtime)
    phase = Phase(
        phase_id="phase-fail",
        adw_id=run.adw_id,
        seq=1,
        params=PhaseParams(
            name="build",
            kind="agent",
            owner="builder",
            description="Exercise the failed runtime path.",
        ),
    )
    monkeypatch.setattr("adw_modules.agents.permissions.snapshot", lambda _run: {})
    with pytest.raises(agents.AgentRuntimeFailure, match="ended as failed"):
        agents.execute(
            run,
            phase,
            AgentCall(output_type=GenericOutput, prompt="build it"),
        )
    assert not any(args[4] is True for args in run.tracer.envelopes)
    agent_end = [event for event in run.tracer.events if event.type == "agent_end"][-1]
    assert agent_end.payload["status"] == "failed"


def test_agent_map_is_schema_v2_and_saved_atomically(tmp_path: Path) -> None:
    run = object.__new__(Run)
    run._agent_map_path = tmp_path / "agent_map.json"
    run.agent_map = {"schema_version": 2, "agents": {}}
    Run.save_agent_map(run, "builder", {"thread_id": "thread-1", "state": "idle"})
    persisted = json.loads(run._agent_map_path.read_text())
    assert persisted["schema_version"] == 2
    assert persisted["agents"]["builder"]["thread_id"] == "thread-1"
    assert not list(tmp_path.glob(".agent_map.*.tmp"))

    run._agent_map_path.write_text(json.dumps({"builder": {"session_id": "old"}}))
    with pytest.raises(RuntimeError, match="schema_version=2"):
        Run._load_agent_map(run)
