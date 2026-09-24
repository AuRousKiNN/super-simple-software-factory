"""Failure-mode coverage for recoverable spec publications and host ownership."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_tickets import repo, git, run_for, phase
from test_codex_m1 import _QueuedRuntime
from adw_modules import agents, permissions, spec_artifacts as sa, tickets
from adw_modules.codex_schema import strict_output_schema
from adw_modules.data_types import (AgentCall, DocumentContext, DocumentDraftOutput,
    DocumentOutput, DocumentRequest, PlanOutput, PlanningTarget, ReviewOutput,
    ReviewDecision, SSSFConfig)


@pytest.fixture
def run(repo):
    lock = permissions.acquire_workspace_lock(repo)
    result = SimpleNamespace(repo_root=repo, adw_id="session-a",
        session_dir=repo / "sessions/session-a", context_handoff_dir=repo / "sessions/session-a/context_handoff",
        cfg=SimpleNamespace(defaults=SimpleNamespace(data_dir=str(repo))), workspace_lock=lock,
        tracer=SimpleNamespace(event=lambda event: None))
    result.context_handoff_dir.mkdir(parents=True)
    yield result
    lock.release()


def plan(run, name="example"):
    path = sa.bind_plan(run, PlanningTarget(spec_dir=f"specs/{name}"))
    (run.repo_root / path).write_text("---\nrevision: 1\n---\n# Example\nREQ-1: increment\n")
    (run.context_handoff_dir / "plan.md").write_text((run.repo_root / path).read_text())
    output = PlanOutput(status="success", spec_path=path)
    assert sa.plan_gate(output, run).passed
    sa.publish_plan(run, output)
    return tickets.spec_work_item(run.repo_root, path)


def drafts(run, context):
    for p, body in [(context.execution_draft_path, "Observed implementation; validation not yet confirmed."),
                    (context.overview_draft_path, "REQ-1 implemented. Verification pending.")]:
        (run.repo_root / p).write_text(body)
    return DocumentDraftOutput(status="success", execution_draft_path=context.execution_draft_path,
        overview_draft_path=context.overview_draft_path,
        artifacts=[context.execution_draft_path, context.overview_draft_path])


def test_planning_identity_conflicts_aliases_and_revision(run):
    with pytest.raises(ValueError, match="exactly one"):
        PlanningTarget()
    for value in ["../escape", "specs/x/y", "specs/../x", "/tmp/x", "specs//x"]:
        with pytest.raises(ValueError):
            sa.bind_plan(run, PlanningTarget(spec_dir=value))
    (run.repo_root / "specs").mkdir()
    (run.repo_root / "specs/alias").symlink_to(run.repo_root)
    with pytest.raises(ValueError, match="symlink"):
        sa.bind_plan(run, PlanningTarget(spec_dir="specs/alias"))
    item = plan(run)
    assert sa.bind_plan(run, PlanningTarget(spec_dir="specs/example")) == item.spec.path
    with pytest.raises(ValueError, match="conflicting"):
        sa.bind_plan(run, PlanningTarget(spec_dir="specs/other"))
    next_run = SimpleNamespace(**vars(run))
    next_run.session_dir = run.repo_root / "sessions/second"
    with pytest.raises(ValueError, match="occupied"):
        sa.bind_plan(next_run, PlanningTarget(spec_dir="specs/example"))
    sa.bind_plan(next_run, PlanningTarget(spec=item.spec.path))
    (run.repo_root / item.spec.path).write_text("---\nrevision: 2\n---\n# Revised\n")
    (run.context_handoff_dir / "plan.md").write_text((run.repo_root / item.spec.path).read_text())
    output = PlanOutput(status="success", spec_path=item.spec.path)
    assert sa.plan_gate(output, next_run).passed
    sa.publish_plan(next_run, output)
    meta, body = sa._read(run.repo_root / "specs/example/README.md")
    assert meta["stale_reasons"] and "尚未形成执行观察" in body
    assert not (run.repo_root / "specs/example/executions").exists()


def test_two_observations_empty_diff_unique_ids_and_replay(run):
    item = plan(run)
    request = DocumentRequest(work_item=item, purpose="Observe state")
    first = sa.prepare(run, request)
    output = sa.publish(run, first, drafts(run, first))
    before = (run.repo_root / output.document_path).read_bytes()
    assert sa.recover(run, first.execution_id) == output
    second = sa.prepare(run, request)
    assert second.execution_id != first.execution_id
    assert second.history[0].path == output.document_path
    assert sa.snapshot(run) == first.snapshot
    second_output = sa.publish(run, second, drafts(run, second))
    assert second_output.document_path != output.document_path
    assert (run.repo_root / output.document_path).read_bytes() == before
    assert sa.prepare(run, request.model_copy(update={"record_requested": False})) is None
    assert len(list((run.repo_root / "specs/example/executions/session-a").glob("*.md"))) == 2


@pytest.mark.parametrize("mutation", ["overview", "spec", "code", "evidence"])
def test_publish_rejects_drift_without_overwriting_overview(run, mutation):
    item = plan(run)
    (run.repo_root / "proof.md").write_text("Original evidence")
    context = sa.prepare(run, DocumentRequest(work_item=item, purpose="Review",
        evidence=[tickets.artifact(run.repo_root, "proof.md")]))
    draft = drafts(run, context)
    path = {"overview": context.overview_path, "spec": item.spec.path, "code": "code.py", "evidence": "proof.md"}[mutation]
    (run.repo_root / path).write_text((run.repo_root / path).read_text() + "\nOutside edit\n")
    overview = (run.repo_root / context.overview_path).read_bytes()
    with pytest.raises(ValueError):
        sa.publish(run, context, draft)
    assert (run.repo_root / context.overview_path).read_bytes() == overview
    assert not (run.repo_root / context.document_path).exists()


def test_report_published_before_overview_crash_recovers_once(run, monkeypatch):
    item = plan(run)
    context = sa.prepare(run, DocumentRequest(work_item=item, purpose="Record"))
    draft = drafts(run, context)
    original = sa._write
    def crash(path, text):
        if path == run.repo_root / context.overview_path:
            raise OSError("simulated crash")
        original(path, text)
    monkeypatch.setattr(sa, "_write", crash)
    with pytest.raises(OSError, match="simulated"):
        sa.publish(run, context, draft)
    journal = json.loads(sa._journal(run, context.execution_id).read_text())
    assert journal["state"] == "report_published"
    report = (run.repo_root / context.document_path).read_bytes()
    monkeypatch.setattr(sa, "_write", original)
    sa.recover(run, context.execution_id)
    assert (run.repo_root / context.document_path).read_bytes() == report
    assert json.loads(sa._journal(run, context.execution_id).read_text())["state"] == "published"
    (run.repo_root / draft.execution_draft_path).write_text("changed replay")
    with pytest.raises(ValueError, match="different content"):
        sa.publish(run, context, draft)
    (run.repo_root / context.document_path).write_text("altered history")
    with pytest.raises(ValueError, match="conflict"):
        sa.recover(run, context.execution_id)


def test_missing_draft_or_input_and_schema_contract(run):
    item = plan(run)
    with pytest.raises(ValueError, match="missing"):
        sa.prepare(run, DocumentRequest(work_item=item, purpose="Observe", evidence=[
            dict(path="missing.md", sha256="0" * 64)]))
    context = sa.prepare(run, DocumentRequest(work_item=item, purpose="Observe"))
    draft = drafts(run, context)
    (run.repo_root / draft.overview_draft_path).write_text("")
    with pytest.raises(ValueError, match="nonempty"):
        sa.publish(run, context, draft)
    for model in [DocumentDraftOutput, DocumentOutput]:
        schema = strict_output_schema(model)
        assert schema["additionalProperties"] is False
    assert DocumentContext.model_validate(context.model_dump()) == context


def test_index_refresh_marks_code_and_incomplete_publication_stale(run):
    item = plan(run)
    context = sa.prepare(run, DocumentRequest(work_item=item, purpose="Observe"))
    sa.publish(run, context, drafts(run, context))
    sa.prepare(run, DocumentRequest(work_item=item, purpose="Interrupted observation"))
    (run.repo_root / "code.py").write_text("different implementation")
    sa.rebuild_index(run)
    overview = (run.repo_root / context.overview_path).read_text()
    assert "实现快照已改变" in overview and "未完成的执行记录" in overview
    assert "尚未确认" in (run.repo_root / "specs/README.md").read_text()


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt])
def test_documenter_ignored_history_violation_is_restored_on_failure(repo, failure):
    run = run_for(repo, "documenter")
    run.cfg.agents[0].writes = ["**"]  # host protections cannot be opted out of
    run.workspace_lock = permissions.acquire_workspace_lock(repo)
    try:
        item = plan(run)
        context = sa.prepare(run, DocumentRequest(work_item=item, purpose="Observe"))
        history = repo / "specs/other/executions/older/report.md"
        history.parent.mkdir(parents=True)
        history.write_text("immutable history")
        (repo / ".gitignore").write_text("sessions/\nspecs/other/\n")
        def malicious(_request):
            history.write_text("forged")
            raise failure("runtime crashed")
        class CrashingRuntime(_QueuedRuntime):
            def run_turn(self, request, hooks):
                return malicious(request)
        run.runtime = CrashingRuntime([])
        with pytest.raises(permissions.PermissionBreach):
            agents.execute(run, phase(run, "documenter"), AgentCall(output_type=DocumentDraftOutput,
                prompt="Observe", document_context=context, work_item=item))
        assert history.read_text() == "immutable history"
        assert not permissions.permitted("specs/other/README.md", run.cfg.agents[0], run.cfg, run)
        assert run.active_report_dir is None
    finally:
        run.workspace_lock.release()


def test_finish_projection_conflict_preserves_real_result(run):
    item = plan(run)
    context = sa.prepare(run, DocumentRequest(work_item=item, purpose="Observe"))
    output = sa.publish(run, context, drafts(run, context))
    sa.prepare_finish(run, output)
    sa.record_finish(run, False)
    run.workspace_lock.release()
    path = run.repo_root / output.overview_path
    path.write_text(path.read_text() + "\nAnother observer's update\n")
    with pytest.raises(ValueError, match="conflict"):
        sa.sync_finished(run)
    saved = json.loads((run.session_dir / "spec-finish.json").read_text())
    assert saved["accepted"] is False and saved["state"] == "pending_projection"
    assert "Another observer" in path.read_text()


def test_committing_docs_does_not_change_observed_snapshot(run, monkeypatch):
    from adw_modules import git_helper
    monkeypatch.chdir(run.repo_root)
    item = plan(run)
    context = sa.prepare(run, DocumentRequest(work_item=item, purpose="Observe"))
    output = sa.publish(run, context, drafts(run, context))
    git(run.repo_root, "add", "code.py")
    git_helper.commit_paths("发布文档", output.artifacts)
    assert sa.snapshot(run) == context.snapshot
    assert (run.repo_root / output.document_path).is_file()


def test_ticket_acceptance_updates_only_ticket_scope_and_replays(run):
    from adw_modules.data_types import AcceptanceRecord, TicketWorkItem
    from adw_modules import review_routing
    root_item = plan(run)
    directory = run.repo_root / "specs/example/spec.tickets"
    directory.mkdir()
    for name in ["ticket-set.md", "TICKET-ONE.md", "index.json"]:
        (directory / name).write_text("{}")
    refs = {name: tickets.artifact(run.repo_root, sa.relative(run, directory / name), Path(name).suffix)
            for name in ["ticket-set.md", "TICKET-ONE.md", "index.json"]}
    item = TicketWorkItem(spec=root_item.spec, ticket_set=refs["ticket-set.md"],
        ticket=refs["TICKET-ONE.md"], index=refs["index.json"], ticket_id="TICKET-ONE", definition_sha256="a" * 64)
    tickets.atomic_json(run.session_dir / "work_item.json", item.model_dump())
    proof = run.context_handoff_dir / "check.log"
    proof.write_text("test passed")
    ref = tickets.artifact(run.repo_root, sa.relative(run, proof), ".log")
    git(run.repo_root, "add", ".")
    git(run.repo_root, "commit", "-qm", "记录实现基线")
    run.accepted = True
    run.workspace_lock.release()
    receipt = tickets.record_acceptance(run, AcceptanceRecord(adw_id=run.adw_id,
        ticket_id=item.ticket_id, definition_sha256=item.definition_sha256,
        baseline=review_routing.baseline(run), checks=[ref], reviews=[ref], applicability="Current code"))
    meta, _ = sa._read(run.repo_root / "specs/example/README.md")
    assert meta["acceptance"]["scope"] == "TICKET-ONE"
    assert meta["acceptance"]["source"] == receipt.path
    assert "整个规格 / 通过" not in (run.repo_root / "specs/README.md").read_text()
    assert json.loads((run.session_dir / "ticket-facts.json").read_text())["state"] == "synced"


@pytest.mark.parametrize("role,output", [("planner", PlanOutput), ("documenter", DocumentDraftOutput)])
def test_role_missing_explicit_target_fails_before_runtime(repo, role, output):
    runtime = _QueuedRuntime([])
    run = run_for(repo, role, runtime)
    with pytest.raises(ValueError, match="requires"):
        agents.execute(run, phase(run, role), AgentCall(output_type=output, prompt="Do work"))
    assert runtime.requests == []


def test_revision_keeps_observation_definition_until_new_document(run):
    item = plan(run)
    context = sa.prepare(run, DocumentRequest(work_item=item, purpose="Observe revision one"))
    output = sa.publish(run, context, drafts(run, context))
    (run.repo_root / item.spec.path).write_text("---\nrevision: 2\n---\n# New scope\nREQ-2\n")
    current = tickets.artifact(run.repo_root, item.spec.path)
    sa.initialize(run, current)
    sa.rebuild_index(run)
    meta, _ = sa._read(run.repo_root / output.overview_path)
    assert meta["revision"] == 1 and meta["current_revision"] == 2
    assert meta["spec_sha256"] == item.spec.sha256 and meta["current_spec_sha256"] == current.sha256
    assert meta["stale_reasons"]


def test_blocked_planner_publishes_valid_goal_without_execution_claim(repo):
    path = "specs/blocked/spec.md"
    text = "---\nrevision: 1\n---\n# Blocked goal\nNeeds a product decision.\n"
    output = PlanOutput(status="fail", spec_path=path, summary="Missing product decision")
    class BlockedRuntime(_QueuedRuntime):
        def run_turn(self, request, hooks):
            (repo / path).write_text(text)
            (run.context_handoff_dir / "plan.md").write_text(text)
            return super().run_turn(request, hooks)
    run = run_for(repo, "planner", BlockedRuntime([output.model_dump_json()]))
    run.workspace_lock = permissions.acquire_workspace_lock(repo)
    try:
        with pytest.raises(agents.AgentReportedFailure):
            agents.execute(run, phase(run, "planner"), AgentCall(output_type=PlanOutput,
                prompt="Plan the blocked goal", planning_target=PlanningTarget(spec_dir="specs/blocked")))
        meta, body = sa._read(repo / "specs/blocked/README.md")
        assert not meta["observed_at"] and not meta["acceptance"]
        assert "尚未形成执行观察" in body
        assert not (repo / "specs/blocked/executions").exists()
    finally:
        run.workspace_lock.release()


def test_clean_explicit_baseline_does_not_replay_last_commit(run, monkeypatch):
    from adw_modules import changes, git_helper
    from adw_modules.data_types import ChangeCapture
    monkeypatch.chdir(run.repo_root)
    (run.repo_root / "code.py").write_text("new committed implementation")
    git(run.repo_root, "add", "code.py")
    git(run.repo_root, "commit", "-qm", "实现已提交功能")
    base = git_helper.rev()
    captured = changes.capture(run, ChangeCapture(base=base))
    assert captured.empty and captured.base.commit == base
    assert "new committed implementation" not in Path(captured.diff_path).read_text()


def test_new_scope_acceptance_clears_old_receipt_freshness_warning(run):
    from adw_modules import review_routing
    from adw_modules.data_types import FinishOptions
    item = plan(run)
    overview = run.repo_root / "specs/example/README.md"
    meta, body = sa._read(overview)
    meta["acceptance"] = {"scope": "整个规格", "result": "通过", "snapshot": "0" * 64}
    sa._write(overview, sa._page(meta, body))
    sa.rebuild_index(run)
    assert "验收回执对应旧实现快照" in overview.read_text()
    run.phases = [SimpleNamespace(seq=1)]
    review = ReviewOutput(status="success", approved=True)
    receipt = review_routing.save(run, review, ReviewDecision(action="approve", reason="All assigned evidence accepted"),
        review_routing.ReceiptContext("Accept current scope", review_routing.baseline(run), item, []))
    ref = tickets.artifact(run.repo_root, sa.relative(run, receipt), ".json")
    context = sa.prepare(run, DocumentRequest(work_item=item, purpose="Current observation", review=review, review_receipt=ref))
    output = sa.publish(run, context, drafts(run, context))
    sa.prepare_finish(run, output, FinishOptions(receipt=ref, accepts_scope=True))
    sa.record_finish(run, True)
    run.workspace_lock.release()
    sa.sync_finished(run)
    meta, _ = sa._read(overview)
    assert not meta["stale_reasons"]
    assert meta["acceptance"]["accepted"]
    assert "/spec-acceptance/" in meta["acceptance"]["source"]
    assert "待复核" not in (run.repo_root / "specs/README.md").read_text()
