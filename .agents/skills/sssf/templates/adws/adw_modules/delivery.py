"""Shared implementation, verification, review, documentation and acceptance chain.

Entrypoints choose a target; this module owns the same delivery policy for build,
planned delivery and evidence-only ticket rechecks. No agent issues acceptance.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import changes, evidence_freshness, gates, git_helper, quality, review_routing, spec_artifacts, tickets
from .data_types import (AcceptanceRecord, AgentCall, ArtifactRef, BuildInput, BuildOutput,
                         ChangeCapture, DocumentRequest, EnvelopeBase, FinishOptions, GenericOutput,
                         PhaseParams, ReviewOutput, ReviewReceipt, ReviewDecision, SpecWorkItem, TicketWorkItem)

REQUIRED_AGENTS = ["scout", "builder", "reviewer", "documenter"]
MAX_REVISION_LOOPS = 3
MAX_VERIFICATION_LOOPS = 2


@dataclass
class DeliveryRequest:
    prompt: str
    work_item: SpecWorkItem | TicketWorkItem
    build_base: str
    previous: EnvelopeBase | None = None


@dataclass
class DeliveryEvidence:
    request: DeliveryRequest
    review: ReviewOutput
    receipt: Path
    results: dict
    mandatory: list[str]
    decision: ReviewDecision
    commit_message: str = "记录验证后的实现与交付文档"
    extra_evidence: tuple[ArtifactRef, ...] = ()
    recheck: bool = False


def preflight(run) -> list[str]:
    """Reject missing commands and unrelated dirty implementation before agents run."""
    required = quality.required_checks()
    tickets._assert_clean_baseline(run)
    sessions = run.session_dir.parent.resolve()
    untracked = [p for p in git_helper.untracked_files()
                 if not (run.repo_root / p).resolve().is_relative_to(sessions)]
    if untracked:
        raise ValueError(f"commit or remove existing untracked inputs before delivery: {untracked}")
    return required


def resolve_input(run, target: BuildInput):
    if target.ticket:
        return tickets.ticket_work_item(run, target.ticket_set, target.ticket, target.dependency_evidence)
    if target.spec:
        return tickets.spec_work_item(run.repo_root, target.spec)
    # A direct request is an immutable spec too, so documentation has a durable home.
    path = f"specs/request-{run.adw_id}/spec.md"
    destination = tickets.repo_path(run.repo_root, path)
    content = "---\nrevision: 1\n---\n\n" + target.prompt + "\n"
    if destination.exists() and destination.read_text() != content:
        raise ValueError("request spec already exists with different content; use a fresh session")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content)
    return tickets.spec_work_item(run.repo_root, path)


def capture(run, base, notes):
    changeset = changes.capture(run, ChangeCapture(base=base))
    return changes.as_envelope(changeset, notes)


def execute(run, request: DeliveryRequest) -> int:
    required = quality.required_checks()
    spec_artifacts._layout(run, request.work_item.spec.path)
    freshness = evidence_freshness.inspect(run, evidence_freshness.dependency_refs(request.work_item), request.work_item)
    reason = evidence_freshness.failure_reason(freshness)
    if reason:
        return run.finish(accepted=False, reason=reason)
    prior = request.previous
    if freshness is not None:
        prior = (prior or GenericOutput(status="success")).model_copy(update={
            "notes_for_next_agent": (prior.notes_for_next_agent + "\n" if prior else "") + evidence_freshness.notes(freshness)})
    tickets.bind_work_item(run, AgentCall(output_type=BuildOutput, prompt=request.prompt,
                                         work_item=request.work_item), "builder")
    with run.phase(PhaseParams(name="build", kind="agent", owner="builder", retries=1,
                               description="Implement only the immutable selected target")) as ph:
        build = ph.call(AgentCall(output_type=BuildOutput, prompt=request.prompt,
                                 work_item=request.work_item, previous=prior))
    repairs = verifications = round_number = 0
    previous_review = None
    results = {}
    pending = required
    while True:
        round_number += 1
        if pending:
            with run.phase(PhaseParams(name=f"checks_{round_number}", kind="code", owner="quality",
                                       description="Execute every required or invalidated check against the current implementation")) as ph:
                result = quality.run_selected(run, pending)
                results.update({c.name: c for c in result.checks})
                ph.log(passed=result.passed, artifacts=result.artifacts)
        with run.phase(PhaseParams(name=f"changes_review_{round_number}", kind="code", owner="git",
                                   description="Capture actual changes and execution evidence for independent review")) as ph:
            results = review_routing.refresh_results(run, results)
            review_input = capture(run, request.build_base, review_routing.evidence_notes(results)
                                   + "\nQuality applicability: " + str(quality.not_applicable_checks())
                                   + "\n" + evidence_freshness.notes(freshness))
            ph.log(diff=review_input.diff_path)
        with run.phase(PhaseParams(name=f"review_{round_number}", kind="agent", owner="reviewer", retries=1,
                                   description="Judge target requirements and manual obligations using scout-validated prior evidence")) as ph:
            review = ph.call(AgentCall(output_type=ReviewOutput, prompt=request.prompt,
                work_item=request.work_item, previous=review_input,
                gates=[gates.artifacts_exist, gates.verdict_consistent, gates.obligations_retained(previous_review)]))
        previous_review = review
        with run.phase(PhaseParams(name=f"route_{round_number}", kind="code", owner="review_routing",
                                   description="Persist review ownership and enforce required checks and bounded repair")) as ph:
            results = review_routing.refresh_results(run, results)
            decision = review_routing.acceptance_decision(review, review_routing.RoutingPolicy(
                repair_remaining=MAX_REVISION_LOOPS - repairs,
                verification_remaining=MAX_VERIFICATION_LOOPS - verifications,
                checks=quality.configured_checks()), results, required)
            receipt = review_routing.save(run, review, decision, review_routing.ReceiptContext(
                request.prompt, request.build_base, request.work_item, sorted(set(required) | set(results))))
            ph.log(receipt=str(receipt), **decision.model_dump())
        if decision.action in {"approve", "handoff"}:
            return complete(run, DeliveryEvidence(request, review, receipt, results, required, decision,
                build.commit_message or "实现需求并记录验证结果"))
        if decision.action == "repair":
            repairs += 1
            with run.phase(PhaseParams(name=f"revise_{repairs}", kind="agent", owner="builder", retries=1,
                                       description="Repair owned implementation defects while retaining the same target")) as ph:
                build = ph.call(AgentCall(output_type=BuildOutput, prompt=request.prompt,
                                         work_item=request.work_item, previous=review))
            pending = sorted(set(required) | set(results) | set(decision.checks))
            results = {}
        else:
            verifications += 1
            pending = decision.checks


def _relative(run, value: str | Path) -> str:
    path = Path(value)
    return path.resolve().relative_to(run.repo_root.resolve()).as_posix() if path.is_absolute() else path.as_posix()


def _assert_unchanged(run, snapshot: str) -> None:
    if review_routing.tree_digest(review_routing.tree_files(run)) != snapshot:
        raise ValueError("implementation changed after verification; recheck before acceptance")


def complete(run, evidence: DeliveryEvidence) -> int:
    """Publish docs before committing; validate unchanged implementation after hooks/finish."""
    item = evidence.request.work_item
    approved = evidence.decision.action == "approve"
    receipt_ref = tickets.artifact(run.repo_root, _relative(run, evidence.receipt), ".json")
    receipt = ReviewReceipt.model_validate_json(evidence.receipt.read_text())
    if approved:
        current = review_routing.refresh_results(run, evidence.results)
        decision = review_routing.acceptance_decision(evidence.review, review_routing.RoutingPolicy(), current, evidence.mandatory)
        if decision.action != "approve":
            return run.finish(accepted=False, reason=decision.reason)
        _assert_unchanged(run, receipt.tree_sha256)
    manual = []
    if approved and isinstance(item, TicketWorkItem):
        # Reviewer obligations must cite actual retained files, not unverifiable prose.
        for obligation in evidence.review.required_verification:
            if not obligation.satisfied or not obligation.evidence:
                raise ValueError(f"required validation has no evidence: {obligation.id}")
            manual.extend(tickets.artifact(run.repo_root, _relative(run, value), Path(value).suffix) for value in obligation.evidence)
        manual.extend(evidence.extra_evidence)
    document = spec_artifacts.document(run, DocumentRequest(work_item=item,
        purpose="记录实施、质量检查、独立审查及未关闭义务。" + evidence.decision.reason,
        changes=capture(run, evidence.request.build_base, evidence.decision.reason),
        checks=list(evidence.results.values()), review=evidence.review, review_receipt=receipt_ref,
        evidence=list(evidence.extra_evidence)))
    if document is None:
        raise ValueError("delivery documentation requires specs/<key>/spec.md; migrate the target layout")
    if approved:
        with run.phase(PhaseParams(name="commit_delivery", kind="code", owner="git",
                                   description="Commit only verified implementation and its published execution record")) as ph:
            _assert_unchanged(run, receipt.tree_sha256)
            paths = git_helper.diff_files("HEAD") + git_helper.untracked_files()
            if evidence.recheck:
                # Rechecks publish current spec facts without committing implementation or supplied proof.
                paths = [p for p in paths if p in document.artifacts]
            sessions = run.session_dir.parent.resolve()
            paths = [p for p in paths if not (run.repo_root / p).resolve().is_relative_to(sessions)]
            ph.log(sha=git_helper.commit_paths(evidence.commit_message + "\n\n" +
                (document.commit_message or "补充执行证据与交付现状"), paths))
            _assert_unchanged(run, receipt.tree_sha256)
            tickets._assert_clean_baseline(run)
    spec_artifacts.prepare_finish(run, document, FinishOptions(receipt=receipt_ref,
        accepts_scope=True, commit=approved))
    publication = None
    if approved and isinstance(item, TicketWorkItem):
        checks = [tickets.artifact(run.repo_root, _relative(run, c.output_artifact), Path(c.output_artifact).suffix) for c in evidence.results.values()]
        manifest = run.session_dir / "delivery-evidence.json"
        tickets.atomic_json(manifest, {"tree_sha256": receipt.tree_sha256,
            "checks": {k: v.model_dump() for k, v in evidence.results.items()},
            "mandatory_checks": evidence.mandatory, "not_applicable": quality.not_applicable_checks(),
            "review": receipt_ref.model_dump(), "manual_validation": [r.model_dump() for r in manual]})
        checks.append(tickets.artifact(run.repo_root, _relative(run, manifest), ".json"))
        publication = dict(checks=checks, reviews=[receipt_ref], manual_validation=manual,
            applicability="Checks and independent review cover this ticket and the unchanged implementation tree "
                          + receipt.tree_sha256 + "; required validation evidence was reviewed for applicability.")
    result = run.finish(accepted=approved, reason=evidence.decision.reason)
    if result or not approved:
        return result
    # finish may commit only host-owned documentation; inspect the final HEAD again.
    try:
        _assert_unchanged(run, receipt.tree_sha256)
        tickets._assert_clean_baseline(run)
        if publication:
            record = AcceptanceRecord(adw_id=run.adw_id, ticket_id=item.ticket_id,
                definition_sha256=item.definition_sha256, baseline=git_helper.rev("HEAD"), **publication)
            tickets.record_acceptance(run, record)
    except Exception as error:
        run.console.note(f"交付验收签发失败，需使用 adw-recheck 重新验证：{error}")
        return 1
    return 0
