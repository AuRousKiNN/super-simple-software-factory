from __future__ import annotations

from types import SimpleNamespace

import pytest
from openai_codex import Codex
from openai_codex.api import TurnHandle
from pydantic import ValidationError

from test_codex_m1 import ROOT, _completed_events
from test_codex_m3 import _config, _install_role
from adw_modules import agents
from adw_modules.agent_codex import CodexRuntime, RuntimeHooks, SDK_VERSION
from adw_modules.data_types import AgentRunRequest, CodexRuntimeConfig


class _WireClient:
    """Stub only app-server I/O; exercise the real pinned SDK serializers."""

    def __init__(self):
        self.calls = []

    def _record(self, method, params):
        self.calls.append((method, params.model_dump(mode="json", by_alias=True)))

    def model_list(self, **_kwargs):
        model = SimpleNamespace(id="gpt-5.6-terra", supported_reasoning_efforts=[
            SimpleNamespace(reasoning_effort="low"),
        ])
        return SimpleNamespace(data=[model])

    def thread_start(self, params):
        self._record("thread/start", params)
        return SimpleNamespace(thread=SimpleNamespace(id="thread-1"))

    def thread_resume(self, thread_id, params):
        assert thread_id == "thread-1"
        self._record("thread/resume", params)
        return SimpleNamespace(thread=SimpleNamespace(id=thread_id))

    def _start_turn(self, thread_id, _input, *, params, for_handle):
        assert thread_id == "thread-1" and for_handle
        self._record("turn/start", params)
        return SimpleNamespace(turn=SimpleNamespace(id="turn-1")), object()

    def close(self):
        pass


@pytest.mark.parametrize("role", [
    "planner", "decomposer", "builder", "scout", "reviewer", "documenter",
])
def test_all_roles_send_auto_review_on_start_resume_and_cached_turn(tmp_path, monkeypatch, role):
    wire = _WireClient()
    sdk = object.__new__(Codex)
    sdk._client = wire
    sdk._init = SimpleNamespace(serverInfo=SimpleNamespace(version=SDK_VERSION))
    monkeypatch.setattr(TurnHandle, "stream", lambda _self: iter(_completed_events("done")))
    runtime = CodexRuntime(CodexRuntimeConfig(), tmp_path,
                           codex_factory=lambda _config: sdk,
                           version_provider=lambda _name: SDK_VERSION)
    request = AgentRunRequest(
        role=role, cwd=str(tmp_path), model="gpt-5.6-terra", effort="low",
        developer_instructions="bounded task", prompt="verify",
        output_schema={"type": "object", "properties": {}},
        raw_output_path=str(tmp_path / "raw.jsonl"),
    )
    try:
        assert runtime.run_turn(request, RuntimeHooks()).status == "completed"
        resumed = request.model_copy(update={"thread_id": "thread-1"})
        assert runtime.run_turn(resumed, RuntimeHooks()).status == "completed"
        runtime._threads.clear()  # Exercise resume from a persisted thread ID.
        assert runtime.run_turn(resumed, RuntimeHooks()).status == "completed"
    finally:
        runtime.close()
    assert [method for method, _ in wire.calls] == [
        "thread/start", "turn/start", "turn/start", "thread/resume", "turn/start",
    ]
    for method, payload in wire.calls:
        assert payload["approvalPolicy"] == "on-request"
        assert payload["approvalsReviewer"] == "auto_review"
        if method == "turn/start":
            assert payload["sandboxPolicy"]["type"] == "workspaceWrite"
            assert payload["sandboxPolicy"]["networkAccess"] is False
        else:
            assert payload["sandbox"] == "workspace-write"


def test_config_requires_explicit_upgrade_from_never():
    assert CodexRuntimeConfig().approval_mode == "auto_review"
    template = agents.load_config(str(ROOT / ".agents/skills/sssf/templates/sssf.config.yaml"))
    assert template.codex.approval_mode == "auto_review"
    with pytest.raises(ValidationError, match="approval_policy"):
        CodexRuntimeConfig(approval_policy="never")
    with pytest.raises(ValidationError, match="approval_mode"):
        CodexRuntimeConfig(approval_mode="deny_all")


@pytest.mark.parametrize("old,new", [
    ('approval_policy = "on-request"', 'approval_policy = "never"'),
    ('approvals_reviewer = "auto_review"', 'approvals_reviewer = "user"'),
    ('approvals_reviewer = "auto_review"', ''),
])
def test_recon_role_cannot_disable_auto_review(tmp_path, monkeypatch, old, new):
    role = _install_role(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = _config(tmp_path)
    agents.validate(cfg, ["planner"])
    role.write_text(role.read_text().replace(old, new))
    with pytest.raises(SystemExit, match="approval_policy=on-request and approvals_reviewer=auto_review"):
        agents.validate(cfg, ["planner"])
