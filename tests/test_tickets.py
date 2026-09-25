from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents/skills/sssf"
sys.path.insert(0, str(SKILL / "templates/adws"))

from adw_modules import agents, permissions, tickets
from adw_modules.codex_schema import strict_output_schema
from adw_modules.data_types import (
    AcceptanceRecord, AgentCall, BuildInput, BuildOutput, DecomposeOutput,
    GenericOutput, Phase, PhaseParams, PlanOutput, SSSFConfig,
)
from test_codex_m1 import _QueuedRuntime, _Run


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "SSSF test")
    git(tmp_path, "config", "user.email", "sssf@example.invalid")
    (tmp_path / ".gitignore").write_text("sessions/\n")
    (tmp_path / "code.py").write_text("original\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "初始化测试")
    return tmp_path


def planning(root, edges=None):
    edges = edges or {"TICKET-Z": [], "TICKET-A": [], "TICKET-M": ["TICKET-Z"]}
    (root / "specs").mkdir(exist_ok=True)
    (root / "specs/root.md").write_text("---\nrevision: 1\n---\n# Root\nREQ-01: query behavior\nCONTRACT-01: stable API\nAC-01: integrated outcome\n")
    output = root / "specs/root.tickets"
    (output / "tickets").mkdir(parents=True, exist_ok=True)
    members = []
    for name, blockers in edges.items():
        member = f"specs/root.tickets/tickets/{name}.md"
        members.append(member)
        write_ticket(root / member, name, blockers)
    meta = {"schema_version": 1, "revision": 1, "source_spec": "specs/root.md", "tickets": members}
    (output / "ticket-set.md").write_text("---\n" + yaml.safe_dump(meta) + "---\n# Tickets\n"
        "## Integration obligations\nAC-01 integration.\n## Coordination\nNone.\n"
        "## Rationale and revision impact\nIndependent behavior.\n")
    return "specs/root.tickets/ticket-set.md", members


def write_ticket(path, name, blockers):
    meta = dict(schema_version=1, id=name, revision=1, kind="behavior", profile="standard",
                blocked_by=blockers, requirements=["REQ-01"])
    path.write_text("---\n" + yaml.safe_dump(meta) + "---\n# Ticket\n"
        "## Behavior and scope\nREQ-01 increment.\n## Contracts and prerequisites\nCONTRACT-01.\n"
        f"## Acceptance criteria\n| AC | Outcome | Evidence |\n| AC-{name}-01 | Result | Test |\n"
        "## Verification and risks\nExisting checks.\n")


def run_for(root, role="builder", runtime=None):
    system = root / f"{role}-system.md"
    user = root / f"{role}-user.md"
    system.write_text("role {{subagent_instructions}}")
    user.write_text("{{prompt}}\n{{work_item}}\n{{decomposition}}\n{{previous_envelope}}")
    cfg = SSSFConfig.model_validate({"schema_version": 2, "defaults": {"data_dir": str(root)},
        "agents": [{"name": role, "writes": ["specs/*.tickets/**"] if role == "decomposer" else None,
                    "prompt_engineering": {"system": str(system), "user": str(user)}}]})
    run = _Run(root, cfg, runtime)
    run.repo_root = root
    return run


def phase(run, role="builder", retries=1):
    return Phase(phase_id="phase", adw_id=run.adw_id, seq=1,
                 params=PhaseParams(name="work", kind="agent", owner=role, retries=retries,
                                    description="Exercise the complete runtime contract"))


def test_graph_order_independence_index_and_staleness(repo):
    set_path, members = planning(repo)
    first = tickets.write_index(repo, set_path)
    data = tickets.load_index(repo, set_path)
    assert [t["id"] for t in data["tickets"] if not t["blocked_by"]] == ["TICKET-A", "TICKET-Z"]
    assert first == tickets.write_index(repo, set_path)
    (repo / members[0]).write_text((repo / members[0]).read_text() + "\nclarification\n")
    with pytest.raises(tickets.TicketError, match="stale index"):
        tickets.load_index(repo, set_path)
    assert tickets.write_index(repo, set_path) != first
    (repo / "specs/root.md").write_text((repo / "specs/root.md").read_text() + "\nnew semantics\n")
    with pytest.raises(tickets.TicketError, match="stale index"):
        tickets.load_index(repo, set_path)


@pytest.mark.parametrize("edges, message", [
    ({"TICKET-Z": ["TICKET-Z"]}, "self dependency"),
    ({"TICKET-Z": ["TICKET-X"]}, "unknown blocker"),
    ({"TICKET-Z": ["TICKET-A"], "TICKET-A": ["TICKET-Z"]}, "cycle"),
    ({"TICKET-Z": ["TICKET-A", "TICKET-A"], "TICKET-A": []}, "duplicate blocker"),
])
def test_reject_invalid_graph(repo, edges, message):
    path, _ = planning(repo, edges)
    with pytest.raises(tickets.TicketError, match=message):
        tickets.read_set(repo, path)


@pytest.mark.parametrize("mutation", [
    lambda s: s.replace("id: TICKET-Z", "id: lowercase_id"),
    lambda s: s.replace("kind: behavior", "kind: custom_kind"),
    lambda s: s.replace("revision: 1", "revision: true"),
    lambda s: s.replace("REQ-01", "free-form-requirement"),
    lambda s: s.replace("## Verification and risks", "## Anything"),
    lambda s: s.replace("profile: standard", "profile: critical"),
    lambda s: s.replace("schema_version: 1", "schema_version: 99\ncustom_field: anything"),
    lambda s: s.replace("id: TICKET-Z", "id: TICKET-Z\nid: TICKET-Z"),
])
def test_artifact_format_is_not_validated(repo, mutation):
    set_path, members = planning(repo, {"TICKET-Z": []})
    path = repo / members[0]
    path.write_text(mutation(path.read_text()))
    assert tickets.read_set(repo, set_path)["tickets"]


def test_membership_paths_and_source_binding(repo):
    set_path, members = planning(repo, {"TICKET-Z": []})
    with pytest.raises(tickets.TicketError, match="POSIX"):
        tickets.artifact(repo, "../outside.md")
    (repo / "alias.md").symlink_to(repo / "specs/root.md")
    with pytest.raises(tickets.TicketError, match="symlink"):
        tickets.artifact(repo, "alias.md")
    duplicate = (repo / set_path).read_text().replace(f"- {members[0]}", f"- {members[0]}\n- {members[0]}")
    (repo / set_path).write_text(duplicate)
    with pytest.raises(tickets.TicketError, match="duplicate set member"):
        tickets.read_set(repo, set_path)
    (repo / set_path).write_text(duplicate.replace(f"- {members[0]}\n- {members[0]}", f"- {members[0]}"))
    (repo / members[0]).unlink()
    with pytest.raises(FileNotFoundError):
        tickets.read_set(repo, set_path)


def test_session_identity_and_definition_binding(repo):
    set_path, members = planning(repo)
    tickets.write_index(repo, set_path)
    run = run_for(repo)
    item = tickets.ticket_work_item(run, set_path, members[0])
    call = AgentCall(output_type=BuildOutput, prompt="build", work_item=item)
    assert tickets.bind_work_item(run, call)[0] == item
    assert tickets.bind_work_item(run, AgentCall(output_type=BuildOutput, prompt="fix"))[0] == item
    other = tickets.ticket_work_item(run, set_path, members[1])
    with pytest.raises(tickets.TicketError, match="session work_item changed"):
        tickets.bind_work_item(run, call.model_copy(update={"work_item": other}))
    (repo / members[0]).write_text((repo / members[0]).read_text() + "changed")
    with pytest.raises(tickets.TicketError, match="artifact changed"):
        tickets.bind_work_item(run, call)


def test_real_runtime_repairs_keep_target_and_feedback(repo):
    planning(repo)
    runtime = _QueuedRuntime([BuildOutput(status="success").model_dump_json()] * 2)
    run = run_for(repo, runtime=runtime)
    item = tickets.spec_work_item(repo, "specs/root.md")
    agents.execute(run, phase(run), AgentCall(output_type=BuildOutput, prompt="build", work_item=item))
    agents.execute(run, phase(run), AgentCall(output_type=BuildOutput, prompt="repair",
                   previous=GenericOutput(status="fail", summary="specific failing check")))
    assert runtime.requests[1].thread_id == runtime.requests[0].thread_id or runtime.requests[1].thread_id == "thread-shared"
    assert all('"kind": "spec"' in request.prompt for request in runtime.requests)
    assert "specific failing check" in runtime.requests[1].prompt
    assert json.loads((run.session_dir / "work_item.json").read_text())["spec"] == item.spec.model_dump()


@pytest.mark.parametrize("error", [None, RuntimeError("failed"), KeyboardInterrupt(), SystemExit("cancelled")])
def test_dynamic_permissions_restore_all_exit_paths(repo, error):
    planning(repo)
    class MutatingRuntime(_QueuedRuntime):
        def run_turn(self, request, hooks):
            (repo / "specs/root.md").write_text("unauthorized rewrite")
            (repo / "sessions/adw-test/work_item.json").write_text("{}")
            if error is not None:
                raise error
            return super().run_turn(request, hooks)
    runtime = MutatingRuntime([BuildOutput(status="success").model_dump_json()])
    run = run_for(repo, runtime=runtime)
    item = tickets.spec_work_item(repo, "specs/root.md")
    with pytest.raises(permissions.PermissionBreach):
        agents.execute(run, phase(run), AgentCall(output_type=BuildOutput, prompt="build", work_item=item))
    assert tickets.artifact(repo, "specs/root.md") == item.spec
    assert json.loads((run.session_dir / "work_item.json").read_text())["spec"] == item.spec.model_dump()
    assert run.active_readonly_paths == []


def test_decomposer_has_no_artifact_format_gate_and_preserves_failure(repo):
    planning(repo, {"TICKET-Z": []})
    runtime = _QueuedRuntime([DecomposeOutput(status="success", outcome="ready",
        ticket_set_path="specs/root.tickets/arbitrary-name.md").model_dump_json()])
    run = run_for(repo, role="decomposer", runtime=runtime)
    target = tickets.prepare_decomposition(run, "specs/root.md")
    # Missing artifacts and arbitrary names do not cause agent format correction.
    output = agents.execute(run, phase(run, "decomposer"), AgentCall(output_type=DecomposeOutput,
                            prompt="decompose", decomposition=target))
    assert output.status == "success"
    assert len(runtime.requests) == 1
    failed = DecomposeOutput(status="fail", outcome="needs_decision", summary="Choose public policy")
    run.runtime = _QueuedRuntime([failed.model_dump_json()])
    with pytest.raises(RuntimeError, match="Choose public policy"):
        agents.execute(run, phase(run, "decomposer"), AgentCall(output_type=DecomposeOutput,
                        prompt="revise", decomposition=target))
    persisted = json.loads((run.session_dir / "decomposer/envelope.json").read_text())
    assert persisted["outcome"] == "needs_decision"
    assert len(run.runtime.requests) == 1


def test_decomposer_can_revise_current_files_but_protects_other_inputs(repo):
    planning(repo)
    run = run_for(repo, "decomposer")
    target = tickets.prepare_decomposition(run, "specs/root.md")
    assert target.output_dir == "specs/root.tickets"
    run.active_write_scope = target.output_dir + "/"
    run.active_readonly_paths = [target.spec.path]
    agent = run.cfg.agents[0]
    assert permissions.permitted(target.output_dir + "/tickets/TICKET-A.md", agent, run.cfg, run)
    assert permissions.permitted("specs/root.tickets/tickets/TICKET-Z.md", agent, run.cfg, run)
    assert not permissions.permitted("specs/other.tickets/tickets/TICKET-Z.md", agent, run.cfg, run)
    assert not permissions.permitted("sessions/adw-test/decomposition-published.json", agent, run.cfg, run)
    assert not permissions.permitted(target.output_dir + "/index.json", agent, run.cfg, run)
    assert not permissions.permitted("specs/root.md", agent, run.cfg, run)
    (repo / "specs/root.md").write_text("changed")
    with pytest.raises(tickets.TicketError, match="source changed"):
        tickets.prepare_decomposition(run, "specs/root.md")


def test_blockers_require_intact_host_evidence_and_definition_across_heads(repo):
    set_path, members = planning(repo)
    tickets.write_index(repo, set_path)
    run = run_for(repo)
    # Commit fixture prompts so dirty-input checks exercise only deliberate changes.
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "保存测试规划")
    with pytest.raises(tickets.TicketError, match="evidence required"):
        tickets.ticket_work_item(run, set_path, members[2])
    evidence_dir = repo / "sessions/accepted-run"
    evidence_dir.mkdir(parents=True)
    for name in ("checks.md", "review.md"):
        (evidence_dir / name).write_text("Executed and accepted on the current baseline.")
    record = AcceptanceRecord(adw_id="accepted-run", ticket_id="TICKET-Z",
        definition_sha256=tickets.load_index(repo, set_path)["definition_sha256"],
        baseline=git(repo, "rev-parse", "HEAD"), applicability="Current code and environment confirmed",
        checks=[tickets.artifact(repo, "sessions/accepted-run/checks.md")],
        reviews=[tickets.artifact(repo, "sessions/accepted-run/review.md")])
    record_path = evidence_dir / "ticket-acceptance.json"
    tickets.atomic_json(record_path, record.model_dump())
    evidence_path = "sessions/evidence.json"
    def refs():
        tickets.atomic_json(repo / evidence_path, [tickets.artifact(repo,
            "sessions/accepted-run/ticket-acceptance.json", ".json").model_dump()])
    refs()
    item = tickets.ticket_work_item(run, set_path, members[2], evidence_path)
    assert item.ticket_id == "TICKET-M"
    (repo / "unrelated.py").write_text("unrelated_feature = True\n")
    git(repo, "add", "unrelated.py")
    git(repo, "commit", "-qm", "添加无关功能")
    assert record.baseline != git(repo, "rev-parse", "HEAD")
    reused = tickets.ticket_work_item(run, set_path, members[2], evidence_path)
    assert reused == item
    tickets.validate_work_item(run, reused)
    original_checks = (evidence_dir / "checks.md").read_text()
    (evidence_dir / "checks.md").write_text("tampered evidence")
    with pytest.raises(tickets.TicketError, match="artifact changed"):
        tickets.ticket_work_item(run, set_path, members[2], evidence_path)
    (evidence_dir / "checks.md").write_text(original_checks)
    record_path.write_text(record.model_copy(update={"definition_sha256": "0" * 64}).model_dump_json())
    refs()
    with pytest.raises(tickets.TicketError, match="stale dependency definition"):
        tickets.ticket_work_item(run, set_path, members[2], evidence_path)


def test_envelopes_and_input_modes():
    assert strict_output_schema(DecomposeOutput)["additionalProperties"] is False
    with pytest.raises(ValueError, match="success requires ready"):
        DecomposeOutput(status="success", outcome="needs_decision")
    for params in ({}, {"prompt": "x", "spec": "y"}, {"ticket_set": "x"}, {"spec": "x", "dependency_evidence": "y"}):
        with pytest.raises(ValueError):
            BuildInput(**params)
    assert BuildInput(ticket="ticket.md").ticket
    assert BuildInput(ticket="ticket.md", ticket_set="ticket-set.md").ticket


def test_install_upgrade_preserves_customizations_and_generated_chain(tmp_path):
    target = tmp_path / "installed"
    def invoke(script, *args):
        result = subprocess.run([sys.executable, str(script), *args], cwd=target if target.exists() else tmp_path,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        return result
    invoke(SKILL / "scripts/install.py", "--root", str(target))
    cfg = agents.load_config(str(target / "adws/adw_sssf_config/sssf.config.yaml"))
    assert agents.resolve(cfg, "decomposer").subagents.enabled
    assert (target / "adws/adw-decompose.py").is_file()
    config = target / "adws/adw_sssf_config/sssf.config.yaml"
    config.write_text("custom config\n")
    invoke(SKILL / "scripts/install.py", "--root", str(target))
    assert config.read_text() == "custom config\n"
    invoke(SKILL / "scripts/make_adw.py", "--name", "tickets", "--agents", "planner,decomposer,builder,reviewer")
    body = (target / "adws/adw-tickets.py").read_text()
    compile(body, "generated", "exec")
    assert "tickets.select_ticket" in body and "selection" in body
    assert "DeliveryRequest(prompt, work_item" in body
    assert "return run.finish(accepted=accepted" in body
    assert "delivery.execute" in body
    assert "delivery.preflight(run)" in body


def test_decomposition_revises_in_place_and_closes_each_published_session(repo):
    set_path, members = planning(repo, {"TICKET-Z": []})
    ticket_path = repo / members[0]
    original_set = (repo / set_path).read_bytes()
    output = DecomposeOutput(status="success", outcome="ready", ticket_set_path=set_path)

    class RevisingRuntime(_QueuedRuntime):
        def run_turn(self, request, hooks):
            if self.requests:
                ticket_path.write_text(ticket_path.read_text().replace("revision: 1", "revision: 2")
                                       + "\nClarified wording.\n")
            return super().run_turn(request, hooks)

    runtime = RevisingRuntime([output.model_dump_json()] * 2)
    run = run_for(repo, "decomposer", runtime)

    @contextmanager
    def workflow_phase(params):
        yield SimpleNamespace(log=lambda **_kwargs: None,
                              call=lambda call: agents.execute(run, phase(run, params.owner), call))

    run.phase = workflow_phase
    assert tickets.decompose(run, "specs/root.md") == output
    first_index = tickets.artifact(repo, "specs/root.tickets/index.json", ".json")
    published = json.loads((run.session_dir / "decomposition-published.json").read_text())
    assert published == first_index.model_dump()
    with pytest.raises(tickets.TicketError, match="already published"):
        tickets.prepare_decomposition(run, "specs/root.md")
    # Completion belongs to this session, even if the shared index is removed.
    (repo / first_index.path).unlink()
    with pytest.raises(tickets.TicketError, match="already published"):
        tickets.prepare_decomposition(run, "specs/root.md")
    tickets.write_index(repo, set_path)
    saved_paths = {p.relative_to(repo) for p in (repo / "specs").rglob("*")}

    run.adw_id = "revise"
    run.session_dir = repo / "sessions/revise"
    run.context_handoff_dir = run.session_dir / "context_handoff"
    run.context_handoff_dir.mkdir(parents=True)
    run.agent_map = {"schema_version": 2, "agents": {}}
    target = tickets.prepare_decomposition(run, "specs/root.md")
    assert target.output_dir == "specs/root.tickets"
    assert tickets.prepare_decomposition(run, "specs/root.md") == target
    assert set(target.model_dump()) == {"spec", "output_dir"}
    assert tickets.decompose(run, "specs/root.md") == output
    data = tickets.load_index(repo, set_path)
    assert data["tickets"][0]["revision"] == 2
    assert (repo / set_path).read_bytes() == original_set
    assert tickets.artifact(repo, first_index.path, ".json") != first_index
    assert {p.relative_to(repo) for p in (repo / "specs").rglob("*")} == saved_paths
    with pytest.raises(tickets.TicketError, match="already published"):
        tickets.prepare_decomposition(run, "specs/root.md")


@pytest.mark.parametrize("kind", ["spec", "ticket"])
def test_in_place_revision_invalidates_old_binding_and_allows_fresh_session(repo, kind):
    set_path, members = planning(repo, {"TICKET-Z": []})
    tickets.write_index(repo, set_path)
    run = run_for(repo)

    def work_item():
        if kind == "spec":
            return tickets.spec_work_item(repo, "specs/root.md")
        return tickets.ticket_work_item(run, set_path, members[0])

    original = work_item()
    tickets.bind_work_item(run, AgentCall(output_type=BuildOutput, prompt="build", work_item=original))
    document = repo / ("specs/root.md" if kind == "spec" else members[0])
    document.write_text(document.read_text().replace("revision: 1", "revision: 2") + "\nClarification.\n")
    tickets.write_index(repo, set_path)
    with pytest.raises(tickets.TicketError, match="artifact changed"):
        tickets.bind_work_item(run, AgentCall(output_type=BuildOutput, prompt="repair"))
    revised = work_item()
    with pytest.raises(tickets.TicketError, match="session work_item changed"):
        tickets.bind_work_item(run, AgentCall(output_type=BuildOutput, prompt="build", work_item=revised))
    run.session_dir = repo / "sessions/revised-build"
    run.session_dir.mkdir()
    assert tickets.bind_work_item(run, AgentCall(output_type=BuildOutput, prompt="build",
                                               work_item=revised))[0] == revised


@pytest.mark.parametrize("failure", ["source", "outside", "failed"])
def test_invalid_decomposition_publication_preserves_previous_index(repo, failure):
    set_path, _ = planning(repo)
    original = tickets.write_index(repo, set_path)
    run = run_for(repo, "decomposer")
    target = tickets.prepare_decomposition(run, "specs/root.md")
    output = DecomposeOutput(status="success", outcome="ready", ticket_set_path=set_path)
    if failure == "source":
        (repo / "specs/root.md").write_text("changed source")
    elif failure == "outside":
        output.ticket_set_path = "specs/other.tickets/ticket-set.md"
    else:
        output = DecomposeOutput(status="fail", outcome="needs_decision")
    with pytest.raises(tickets.TicketError):
        tickets.publish_decomposition(run, target, output)
    assert tickets.artifact(repo, original.path, ".json") == original
    assert not (run.session_dir / "decomposition-published.json").exists()


def test_ac_inline_code_is_valid_markdown(repo):
    path, members = planning(repo, {"TICKET-Z": []})
    ticket = repo / members[0]
    ticket.write_text(ticket.read_text().replace("| AC-TICKET-Z-01 |", "| `AC-TICKET-Z-01` |"))
    assert tickets.read_set(repo, path)["tickets"][0]["id"] == "TICKET-Z"


def test_acceptance_publication_requires_accepted_run_and_committed_implementation(repo):
    set_path, members = planning(repo, {"TICKET-Z": []})
    tickets.write_index(repo, set_path)
    run = run_for(repo)
    item = tickets.ticket_work_item(run, set_path, members[0])
    tickets.bind_work_item(run, AgentCall(output_type=BuildOutput, prompt="implement", work_item=item))
    evidence = run.session_dir / "context_handoff/checks.md"
    evidence.write_text("Executed exact outcome assertion; review and manual obligations checked.")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "保存实现与规格")
    ref = tickets.artifact(repo, evidence.relative_to(repo).as_posix())
    record = AcceptanceRecord(adw_id=run.adw_id, ticket_id=item.ticket_id,
        definition_sha256=item.definition_sha256, baseline=git(repo, "rev-parse", "HEAD"),
        checks=[ref], reviews=[ref], applicability="Current environment checked by ADW")
    with pytest.raises(tickets.TicketError, match="finished, accepted"):
        tickets.record_acceptance(run, record)
    run.accepted = True
    (repo / "code.py").write_text("uncommitted implementation")
    with pytest.raises(tickets.TicketError, match="uncommitted tracked"):
        tickets.record_acceptance(run, record)
    (repo / "code.py").write_text("original\n")
    published = tickets.record_acceptance(run, record)
    assert published.path.endswith("/ticket-acceptance.json")
    assert json.loads((repo / published.path).read_text())["accepted"] is True


def test_current_planning_writes_preserve_host_receipts_and_implementation_inputs(repo):
    set_path, members = planning(repo)
    tickets.write_index(repo, set_path)
    run = run_for(repo, "decomposer")
    target = tickets.prepare_decomposition(run, "specs/root.md")
    run.active_write_scope = target.output_dir + "/"
    run.active_readonly_paths = [target.spec.path]
    # Host receipts are protected even under an ignored session directory.
    receipt = repo / "sessions/previous/decomposition-published.json"
    tickets.atomic_json(receipt, {"path": "specs/root.tickets/index.json", "sha256": "a" * 64})
    original = receipt.read_bytes()
    before = permissions.snapshot(run)
    revised = (repo / members[0]).read_text().replace("revision: 1", "revision: 2")
    (repo / members[0]).write_text(revised)
    receipt.write_text("{}")
    try:
        with pytest.raises(permissions.PermissionBreach):
            permissions.enforce(run, phase(run, "decomposer"), run.cfg.agents[0], before)
        assert receipt.read_bytes() == original
        assert (repo / members[0]).read_text() == revised
    finally:
        before.cleanup()
    tickets.write_index(repo, set_path)
    item = tickets.ticket_work_item(run, set_path, members[0])
    run.active_readonly_paths = tickets.validate_work_item(run, item)
    run.active_write_scope = None
    run.cfg.agents[0].writes = ["specs/"]
    for path in [set_path, *members, "specs/root.md", "specs/root.tickets/index.json"]:
        assert not permissions.permitted(path, run.cfg.agents[0], run.cfg, run)


def test_planner_feedback_does_not_bind_decomposer_to_whole_spec(repo):
    path, members = planning(repo, {"TICKET-Z": []})
    tickets.write_index(repo, path)
    run = run_for(repo, "decomposer")
    call = AgentCall(output_type=DecomposeOutput, prompt="split",
                     previous=PlanOutput(status="success", spec_path="specs/root.md"))
    assert tickets.bind_work_item(run, call, "decomposer") == (None, [])
    assert not (run.session_dir / "work_item.json").exists()
    item = tickets.ticket_work_item(run, path, members[0])
    assert tickets.bind_work_item(run, AgentCall(output_type=BuildOutput, prompt="build", work_item=item))[0] == item


def test_body_is_opaque_and_metadata_has_no_strict_schema(repo):
    path, members = planning(repo, {"TICKET-Z": []})
    (repo / members[0]).write_text("An ordinary document without frontmatter, headings, or AC IDs.\n")
    set_file = repo / path
    text = set_file.read_text().replace("schema_version: 1", "schema_version: anything\nextra: allowed")
    set_file.write_text(text.split("# Tickets")[0] + "Free-form prose.\n")
    payload = tickets.read_set(repo, path)
    assert payload["tickets"][0]["id"] == "TICKET-Z"
    assert payload["tickets"][0]["blocked_by"] == []


def test_signal_keeps_lock_until_permission_restoration(repo, monkeypatch):
    import signal
    from adw_modules import session
    run = run_for(repo)
    run.workspace_lock = permissions.acquire_workspace_lock(repo)
    handlers = {}
    monkeypatch.setattr(session.signal, "signal", lambda sig, handler: handlers.update({sig: handler}))
    monkeypatch.setattr(session.atexit, "register", lambda _callback: None)
    run.close = run.workspace_lock.release
    run.tracer.session_finish = lambda *_args, **_kwargs: None
    run.cfg.agents[0].writes = []
    before = permissions.snapshot(run)
    (repo / "code.py").write_text("unauthorized")
    try:
        session._finalize_when_killed(run)
        with pytest.raises(SystemExit):
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        run.workspace_lock.assert_held()
        with pytest.raises(permissions.PermissionBreach):
            permissions.enforce(run, phase(run), run.cfg.agents[0], before)
        assert (repo / "code.py").read_text() == "original\n"
    finally:
        before.cleanup()
        run.close()
