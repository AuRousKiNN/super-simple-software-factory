from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents/skills/sssf"
sys.path.insert(0, str(SKILL / "templates/adws"))

adw_build = __import__("adw-build")
adw_recheck = __import__("adw-recheck")
adw_simple_sdlc = __import__("adw-simple-sdlc")
from adw_modules import agents, gates, permissions, quality, review_routing as routing, tickets
from adw_modules.codex_schema import strict_output_schema
from adw_modules.data_types import (
    AgentCall, BuildOutput, DocumentOutput, Phase, PhaseParams, PlanOutput,
    QualityCheckSpec, RecheckEvidence, RecheckRequest, ReviewBlocker,
    ReviewFinding, ReviewObligation, ReviewOutput,
)
from test_codex_m1 import _QueuedRuntime
from test_tickets import git, phase, planning, repo, run_for


def blocker(kind="implementation", **overrides):
    data = dict(id="B-1", kind=kind, owner=routing.BLOCKER_OWNERS[kind],
                description="Required behavior has a concrete gap", basis=["REQ-1"],
                trigger="Request the empty collection", consequence="The public result is incorrect",
                evidence=["src.py: empty branch returns None"], closure="Return an empty collection",
                handoff="Responsible owner must supply the required result")
    if kind == "check_execution":
        data["checks"] = ["test"]
    if kind == "manual_validation":
        data.update(preconditions="A staging account", steps=["Open the empty collection"],
                    pass_criteria="The screen shows an empty list")
    data.update(overrides)
    return ReviewBlocker(**data)


def review(*blockers):
    return ReviewOutput(status="success", approved=not blockers,
                        findings=[ReviewFinding(requirement="REQ-1", met=not blockers, evidence="src.py and command.log")],
                        blocking=list(blockers))


@pytest.mark.parametrize("kind,action", [
    ("implementation", "repair"), ("test_implementation", "repair"),
    ("check_execution", "verify"), ("spec_conflict", "handoff"),
    ("ticket_conflict", "handoff"), ("environment", "handoff"),
    ("manual_validation", "handoff"), ("external_regression", "handoff"),
    ("protocol_issue", "handoff"),
])
def test_ownership_routes(kind, action):
    output = review(blocker(kind))
    assert gates.verdict_consistent(output, None).passed
    decision = routing.decide(output, routing.RoutingPolicy(2, 2, {"test"}))
    assert decision.action == action
    assert decision.owners == [routing.BLOCKER_OWNERS[kind]]


def test_mixed_problems_require_all_capabilities_and_prioritize_root_contract():
    output = review(blocker(), blocker("spec_conflict", id="B-2"), blocker("environment", id="B-3"))
    decision = routing.decide(output, routing.RoutingPolicy(3, 2, {"test"}))
    assert decision.action == "handoff" and decision.owners[0] == "planner"
    assert all(b.id in decision.reason for b in output.blocking)
    output = review(blocker(), blocker("check_execution", id="B-2"))
    assert routing.decide(output, routing.RoutingPolicy(3, 0, {"test"})).action == "handoff"
    assert routing.decide(output, routing.RoutingPolicy(3, 2)).action == "handoff"
    assert routing.decide(output, routing.RoutingPolicy(3, 2, {"test"})).action == "repair"
    assert "repair budget" in routing.decide(review(blocker()), routing.RoutingPolicy()).reason


@pytest.mark.parametrize("change", [
    lambda r: r.model_copy(update={"approved": True}),
    lambda r: r.model_copy(update={"blocking": []}),
    lambda r: r.model_copy(update={"blocking": [r.blocking[0], r.blocking[0]]}),
    lambda r: r.model_copy(update={"blocking": [r.blocking[0].model_copy(update={"owner": "planner"})]}),
    lambda r: r.model_copy(update={"blocking": [r.blocking[0].model_copy(update={"evidence": [" "]})]}),
    lambda r: r.model_copy(update={"blocking": [r.blocking[0].model_copy(update={"basis": ["different"]})]}),
    lambda r: r.model_copy(update={"blocking": [blocker("manual_validation", steps=[])]}),
    lambda r: r.model_copy(update={"blocking": [blocker("check_execution", checks=[])]}),
    lambda r: r.model_copy(update={"blocking": [blocker(invalidated_evidence=["old record"], affected_tickets=[])]}),
])
def test_gate_rejects_inconsistent_structured_claims(change):
    assert not gates.verdict_consistent(change(review(blocker())), None).passed


def test_mandatory_verification_and_schema_contract():
    output = review()
    output.required_verification = [ReviewObligation(id="V-1", description="Required manual validation", satisfied=False)]
    assert not gates.verdict_consistent(output, None).passed
    output.approved = False
    output.blocking = [blocker("manual_validation", basis=["V-1"])]
    assert gates.verdict_consistent(output, None).passed
    current = review()
    assert not gates.obligations_retained(output)(current, None).passed
    current.required_verification = [output.required_verification[0].model_copy(update={"satisfied": True, "evidence": ["manual.log"]})]
    assert gates.obligations_retained(output)(current, None).passed
    assert gates.verdict_consistent(current, None).passed
    schema = strict_output_schema(ReviewOutput)
    assert schema["$defs"]["ReviewBlocker"]["additionalProperties"] is False
    with pytest.raises(ValueError):
        ReviewOutput(status="success", blocking=["legacy blocker"])
    # The distribution prompt example stays parseable against the exact output type.
    prompt = (SKILL / "templates/prompt_engineering/reviewer/user.md").read_text()
    example = json.loads(prompt.split("```json\n")[1].split("```", 1)[0])
    assert gates.verdict_consistent(ReviewOutput.model_validate(example), None).passed


def test_business_rejection_reaches_caller_runtime_fail_still_stops(repo):
    output = review(blocker("environment"))
    run = run_for(repo, "reviewer", _QueuedRuntime([output.model_dump_json()]))
    result = agents.execute(run, phase(run, "reviewer"), AgentCall(
        output_type=ReviewOutput, prompt="Review", gates=[gates.verdict_consistent]))
    assert result.status == "success" and result.approved is False
    failed = output.model_copy(update={"status": "fail", "summary": "review execution failed"})
    run.runtime = _QueuedRuntime([failed.model_dump_json()])
    with pytest.raises(RuntimeError, match="review execution failed"):
        agents.execute(run, phase(run, "reviewer"), AgentCall(output_type=ReviewOutput, prompt="Review"))


def test_consistency_correction_uses_same_runtime_thread(repo):
    invalid = review(blocker()).model_copy(update={"approved": True})
    runtime = _QueuedRuntime([invalid.model_dump_json(), review().model_dump_json()])
    run = run_for(repo, "reviewer", runtime)
    assert agents.execute(run, phase(run, "reviewer"), AgentCall(
        output_type=ReviewOutput, prompt="Review", gates=[gates.verdict_consistent])).approved
    assert len(runtime.requests) == 2
    assert runtime.requests[0].thread_id is None
    assert runtime.requests[1].thread_id == "thread-shared"


from adw_modules import spec_artifacts
from adw_modules.data_types import DocumentDraftOutput, PlanningTarget, WorkflowOptions


class Flow:
    """Exercise actual ADW branches, Git capture, quality commands and host receipts."""
    def __init__(self, root, reviews=(), name="flow"):
        self.repo_root = root
        self.adw_id = name
        self.engineer = "test"
        self.cfg = SimpleNamespace(defaults=SimpleNamespace(data_dir=str(root)))
        self.session_dir = root / "sessions" / name
        self.context_handoff_dir = self.session_dir / "context_handoff"
        self.context_handoff_dir.mkdir(parents=True)
        self.phases = []
        self.calls = []
        self.logs = []
        self.reviews = iter(reviews)
        self.tracer = SimpleNamespace(event=lambda event: None)
        self.console = SimpleNamespace(note=lambda text: None)
        self.closed = False
        self.workspace_lock = SimpleNamespace(assert_held=lambda: None)

    @contextmanager
    def phase(self, params):
        seq = len(self.phases) + 1
        ph = Phase(phase_id=f"{self.adw_id}_{seq}", adw_id=self.adw_id, seq=seq, params=params)
        self.phases.append(ph)
        yield SimpleNamespace(call=lambda call: self.call(params, call), log=lambda **kw: self.logs.append(kw))
        ph.status = "success"

    def call(self, params, call):
        self.calls.append((params, call))
        if params.owner == "planner":
            path = spec_artifacts.bind_plan(self, call.planning_target)
            (self.repo_root / path).write_text("---\nrevision: 1\n---\nREQ-1: return a collection\n")
            (self.context_handoff_dir / "plan.md").write_text((self.repo_root / path).read_text())
            result = PlanOutput(status="success", spec_path=path, commit_message="添加需求定义")
            spec_artifacts.publish_plan(self, result)
        elif params.owner == "builder":
            count = sum(p.owner == "builder" for p, c in self.calls)
            (self.repo_root / "code.py").write_text(f"value = {count}\n")
            result = BuildOutput(status="success", commit_message="实现集合返回值")
        elif params.owner == "reviewer":
            report = self.context_handoff_dir / "review.md"
            report.write_text("Original proof and independent review")
            result = next(self.reviews).model_copy(update={"artifacts": [report.relative_to(self.repo_root).as_posix()]})
        elif params.owner == "documenter":
            context = call.document_context
            for path in [context.execution_draft_path, context.overview_draft_path]:
                (self.repo_root / path).write_text("Observed collection behavior; check evidence and unresolved obligations.")
            result = DocumentDraftOutput(status="success", execution_draft_path=context.execution_draft_path,
                overview_draft_path=context.overview_draft_path,
                artifacts=[context.execution_draft_path, context.overview_draft_path], commit_message="补充交付说明")
        else:
            raise AssertionError(params.owner)
        for gate in call.gates:
            assert gate(result, self).passed
        return result

    def finish(self, accepted=True, reason=""):
        self.accepted = accepted
        self.reason = reason
        self.closed = True
        spec_artifacts.record_finish(self, accepted)
        spec_artifacts.sync_finished(self)
        return 0 if accepted else 1


def setup_flow(monkeypatch, repo, reviews, module=adw_build):
    monkeypatch.chdir(repo)
    run = Flow(repo, reviews)
    monkeypatch.setattr(module.agents, "load_config", lambda _: run.cfg)
    monkeypatch.setattr(module.agents, "validate", lambda *_: None)
    monkeypatch.setattr(module.session, "ensure", lambda *_: run)
    spec = QualityCheckSpec(name="test", area="backend", operation="build",
                            argv=[sys.executable, "-c", "print('required assertions executed')"])
    monkeypatch.setattr(quality, "check_specs", lambda: {"test": spec})
    return run


@pytest.mark.parametrize("kind", ["spec_conflict", "ticket_conflict", "environment", "manual_validation", "external_regression", "protocol_issue"])
def test_blocked_workflow_stops_without_builder_repair(monkeypatch, repo, kind):
    run = setup_flow(monkeypatch, repo, [review(blocker(kind))])
    assert adw_build.main("Implement collection") == 1
    assert [p.owner for p, c in run.calls] == ["builder", "reviewer", "documenter"]
    receipt_path = next((run.session_dir / "review-routing").glob("*.json"))
    receipt = routing.ReviewReceipt.model_validate_json(receipt_path.read_text())
    assert receipt.review.blocking[0].kind == kind
    assert receipt.reports and receipt.decision.reason in run.reason
    assert run.closed


def test_repairs_and_verifications_use_separate_budgets(monkeypatch, repo):
    run = setup_flow(monkeypatch, repo, [review(blocker("check_execution")), review(blocker()), review()])
    assert adw_build.main("Implement collection") == 0
    names = [p.params.name for p in run.phases]
    assert names.index("checks_2") < names.index("review_2") < names.index("revise_1") < names.index("checks_3") < names.index("review_3")
    assert len(list((run.session_dir / "review-routing").glob("*.json"))) == 3
    assert json.loads(sorted((run.session_dir / "review-routing").glob("*.json"))[0].read_text())["review"]["blocking"]


@pytest.mark.parametrize("kind,phase_prefix,budget", [("implementation", "revise_", 3), ("check_execution", "checks_", 3)])
def test_loop_limits_count_actual_actions(monkeypatch, repo, kind, phase_prefix, budget):
    run = setup_flow(monkeypatch, repo, [review(blocker(kind))] * 8)
    assert adw_build.main("Implement collection") == 1
    assert sum(p.params.name.startswith(phase_prefix) for p in run.phases) == budget
    assert "budget" in run.reason


def test_sdlc_retests_before_review_and_commits_only_approved_code(monkeypatch, repo):
    run = setup_flow(monkeypatch, repo, [review(blocker()), review()], adw_simple_sdlc)
    assert adw_simple_sdlc.main("Implement collection", target=PlanningTarget(spec_dir="specs/collection")) == 0
    names = [p.params.name for p in run.phases]
    assert names.index("revise_1") < names.index("checks_2") < names.index("review_2") < names.index("document") < names.index("commit_delivery")
    review_calls = [c for p, c in run.calls if p.owner == "reviewer"]
    assert "required assertions executed" in review_calls[-1].previous.notes_for_next_agent
    assert review_calls[0].work_item == review_calls[1].work_item
    assert git(repo, "log", "-1", "--format=%s") == "同步规格验收事实与索引"


def test_sdlc_rejection_and_failed_tests_cannot_deliver(monkeypatch, repo):
    run = setup_flow(monkeypatch, repo, [review()], adw_simple_sdlc)
    spec = QualityCheckSpec(name="test", area="backend", operation="build", argv=[sys.executable, "-c", "raise SystemExit(1)"])
    monkeypatch.setattr(quality, "check_specs", lambda: {"test": spec})
    assert adw_simple_sdlc.main("Implement collection", target=PlanningTarget(spec_dir="specs/collection")) == 1
    assert "failed" in run.reason
    assert any(p.owner == "documenter" for p, c in run.calls)
    assert (repo / "code.py").read_text() == "value = 1\n"
    assert "code.py" in git(repo, "status", "--porcelain")
    meta, _ = spec_artifacts._read(repo / "specs/collection/README.md")
    assert meta["acceptance"]["result"] == "未验收"


def source_receipt(repo):
    (repo / "specs/collection").mkdir(parents=True, exist_ok=True)
    (repo / "specs/collection/spec.md").write_text("---\nrevision: 1\n---\nREQ-1")
    source = Flow(repo, name="original")
    output = review(blocker("manual_validation"))
    output.required_verification = [ReviewObligation(id="V-1", description="Required manual validation", satisfied=False)]
    output.blocking[0].basis.append("V-1")
    with source.phase(PhaseParams(name="route", kind="code", owner="review_routing", description="Save the original review for follow-up")):
        path = routing.save(source, output, routing.decide(output, routing.RoutingPolicy()),
                            routing.ReceiptContext("Implement collection", routing.baseline(source), tickets.spec_work_item(repo, "specs/collection/spec.md")))
    evidence = repo / "evidence.md"
    evidence.write_text("Executed all steps on the current baseline; expected screen observed")
    request = RecheckRequest(original_review=tickets.artifact(repo, path.relative_to(repo).as_posix(), ".json"),
                             baseline=routing.baseline(source), evidence=[RecheckEvidence(
                                 artifact=tickets.artifact(repo, "evidence.md"), resolves=["B-1"], applicability="Current code and staging account")])
    return source, request


def test_recheck_evidence_only_starts_with_checks_or_review_never_builder(monkeypatch, repo):
    monkeypatch.chdir(repo)
    source, request = source_receipt(repo)
    current = review()
    current.required_verification = [ReviewObligation(id="V-1", description="Required manual validation", satisfied=True, evidence=["evidence.md"])]
    run = Flow(repo, [current], "recheck")
    monkeypatch.setattr(adw_recheck.agents, "load_config", lambda _: run.cfg)
    monkeypatch.setattr(adw_recheck.agents, "validate", lambda *_: None)
    monkeypatch.setattr(adw_recheck.session, "ensure", lambda *_: run)
    request_path = repo / "recheck.json"
    request_path.write_text(request.model_dump_json())
    assert adw_recheck.main(str(request_path)) == 0
    assert [p.owner for p, c in run.calls] == ["reviewer", "documenter"]
    assert run.logs[0]["changed"] is False
    assert request.original_review.path in run.calls[0][1].previous.notes_for_next_agent
    assert run.calls[0][1].work_item == tickets.spec_work_item(repo, "specs/collection/spec.md")


@pytest.mark.parametrize("mutation,error", [
    ("definition", "artifact changed"), ("evidence", "artifact changed"),
    ("baseline", "current full Git HEAD"), ("code", "affected configured checks"),
    ("missing_check", "capability missing"), ("blocker", "name original blockers"),
])
def test_recheck_rejects_invalid_or_inapplicable_inputs(monkeypatch, repo, mutation, error):
    monkeypatch.chdir(repo)
    source, request = source_receipt(repo)
    if mutation == "definition":
        (repo / "specs/collection/spec.md").write_text("changed definition")
    elif mutation == "evidence":
        (repo / "evidence.md").write_text("changed proof")
    elif mutation == "baseline":
        request.baseline = "0" * 40
    elif mutation == "code":
        (repo / "code.py").write_text("changed implementation")
    elif mutation == "missing_check":
        request.checks = ["unconfigured"]
    else:
        request.evidence[0].resolves = ["unknown"]
    run = Flow(repo, name="recheck")
    with run.phase(PhaseParams(name="input", kind="code", owner="review_routing", description="Validate explicit recheck inputs")):
        with pytest.raises(ValueError, match=error):
            routing.prepare_recheck(run, request, set())
    assert not (run.session_dir / "work_item.json").exists()


def test_recheck_changed_code_requires_current_checks(monkeypatch, repo):
    monkeypatch.chdir(repo)
    source, request = source_receipt(repo)
    (repo / "code.py").write_text("changed implementation")
    request.checks = ["test"]
    run = Flow(repo, name="recheck")
    with run.phase(PhaseParams(name="input", kind="code", owner="review_routing", description="Validate explicit recheck inputs")):
        prepared = routing.prepare_recheck(run, request, {"test"})
    assert prepared.changed and prepared.checks == ["test"]
    assert (run.session_dir / "work_item.json").exists()


@pytest.mark.parametrize("approved", [False, True])
def test_generated_chain_stops_or_delivers_on_verdict(monkeypatch, repo, approved):
    subprocess.run([sys.executable, str(SKILL / "scripts/make_adw.py"), "--name", "generated",
                    "--agents", "builder,reviewer,documenter"], cwd=repo, check=True, capture_output=True)
    git(repo, "add", "adws")
    git(repo, "commit", "-qm", "记录生成的工作流")
    spec = importlib.util.spec_from_file_location("generated", repo / "adws/adw-generated.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec.loader.exec_module(module)
    output = review() if approved else review(blocker("environment"))
    run = setup_flow(monkeypatch, repo, [output], module)
    (repo / "specs/collection").mkdir(parents=True)
    (repo / "specs/collection/spec.md").write_text("---\nrevision: 1\n---\nREQ-1")
    git(repo, "add", "specs")
    git(repo, "commit", "-qm", "记录交付规格")
    assert module.main("Implement collection", WorkflowOptions(spec="specs/collection/spec.md")) == (0 if approved else 1)
    assert any(p.owner == "documenter" for p, c in run.calls)
    assert run.closed


def test_placeholder_is_not_verification_and_no_arbitrary_command_dispatch(repo):
    run = Flow(repo)
    assert quality.configured_checks() == set()
    with run.phase(PhaseParams(name="test", kind="code", owner="quality", description="Exercise the default placeholder result")):
        assert not quality.run_tests(run).passed
        with pytest.raises(ValueError, match="configured"):
            quality.run_selected(run, ["rm -rf /"])


def test_ignored_review_receipt_is_protected_from_agents(repo):
    source, request = source_receipt(repo)
    run = run_for(repo)
    # The fixture uses data_dir=root, matching host receipt locations.
    agent = agents.resolve(run.cfg, "builder")
    receipt_path = tickets.repo_path(repo, request.original_review.path)
    before = receipt_path.read_bytes()
    assert not permissions.permitted(request.original_review.path, agent, run.cfg, run)
    snap = permissions.snapshot(run)
    receipt_path.write_text("forged approval")
    try:
        with pytest.raises(permissions.PermissionBreach):
            permissions.enforce(run, phase(run), agent, snap)
        assert receipt_path.read_bytes() == before
    finally:
        snap.cleanup()


def test_successful_checks_lose_applicability_after_code_or_configuration_changes(monkeypatch, repo):
    monkeypatch.chdir(repo)
    run = Flow(repo)
    spec = QualityCheckSpec(name="test", area="backend", operation="build", argv=[sys.executable, "-c", "print('passed')"])
    monkeypatch.setattr(quality, "check_specs", lambda: {"test": spec})
    with run.phase(PhaseParams(name="checks", kind="code", owner="quality", description="Execute a real successful check")):
        result = quality.run_selected(run, ["test"])
    results = routing.refresh_results(run, {c.name: c for c in result.checks})
    assert routing.acceptance_decision(review(), routing.RoutingPolicy(), results, ["test"]).action == "approve"
    (repo / "code.py").write_text("different behavior")
    results = routing.refresh_results(run, results)
    assert results["test"].passed and not results["test"].applicable
    assert routing.acceptance_decision(review(), routing.RoutingPolicy(), results, ["test"]).action == "handoff"


def test_recheck_planning_sources_and_reused_sessions_are_rejected(monkeypatch, repo):
    monkeypatch.chdir(repo)
    source, request = source_receipt(repo)
    source_path = tickets.repo_path(repo, request.original_review.path)
    receipt = routing.ReviewReceipt.model_validate_json(source_path.read_text())
    receipt.review = review(blocker("spec_conflict"))
    receipt.decision = routing.decide(receipt.review, routing.RoutingPolicy())
    source_path.write_text(receipt.model_dump_json())
    request.original_review = tickets.artifact(repo, request.original_review.path, ".json")
    run = Flow(repo, name="recheck")
    with run.phase(PhaseParams(name="input", kind="code", owner="review_routing", description="Validate the planning handoff source")):
        with pytest.raises(ValueError, match="planning revision"):
            routing.prepare_recheck(run, request, set())
        run.phases[0].seq = 9
        with pytest.raises(ValueError, match="fresh session"):
            routing.prepare_recheck(run, request, set())


def test_quality_timeout_preserves_byte_output(monkeypatch, repo):
    run = Flow(repo)
    spec = QualityCheckSpec(name="test", area="backend", operation="build", argv=["fake-test"], timeout_seconds=1)
    original_run = subprocess.run
    def timeout(argv, **kwargs):
        if argv == ["fake-test"]:
            raise subprocess.TimeoutExpired(argv, 1, output=b"partial stdout", stderr=b"partial stderr")
        return original_run(argv, **kwargs)
    monkeypatch.setattr(quality.subprocess, "run", timeout)
    monkeypatch.setattr(quality, "check_specs", lambda: {"test": spec})
    with run.phase(PhaseParams(name="timeout", kind="code", owner="quality", description="Retain evidence when a known check times out")):
        result = quality.run_selected(run, ["test"])
    assert not result.passed and result.checks[0].returncode == 124
    assert "partial stdout" in Path(result.artifacts[0]).read_text()
