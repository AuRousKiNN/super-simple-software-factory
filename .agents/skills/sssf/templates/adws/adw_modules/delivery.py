"""Shared implementation, verification, review, documentation and acceptance chain.

Entrypoints choose a target; this module owns the same delivery policy for build,
planned delivery and evidence-only ticket rechecks. No agent issues acceptance.
"""
from __future__ import annotations

import json

from dataclasses import dataclass
from pathlib import Path

from . import changes, gates, git_helper, quality, recovery, review_routing, spec_artifacts, tickets
from .data_types import (AcceptanceRecord, AgentCall, ArtifactRef, BuildInput, BuildOutput,
                         ChangeCapture, DocumentRequest, EnvelopeBase, FinishOptions,
                         PhaseParams, ReviewOutput, ReviewReceipt, ReviewDecision, SpecWorkItem, TicketWorkItem)

REQUIRED_AGENTS = ["builder", "reviewer", "documenter"]
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


def freeze_input(run, request):
    """Bind before scout without projecting implementation-in-progress documents."""
    item = request.work_item
    restored = getattr(run, "delivery_recovery", None)
    tickets.validate_work_item(run, item, check_tree=False)
    if isinstance(item, TicketWorkItem):
        if not restored:
            tickets._assert_clean_baseline(run)
        for ref in (item.spec, item.ticket_set, item.ticket, item.index):
            tracked = tickets._git(run.repo_root, "ls-files", "--", ref.path)
            if tracked != ref.path:
                raise tickets.TicketError(f"bound planning input must be committed: {ref.path}")
    saved = run.session_dir / "delivery-input.json"
    config = run.cfg.model_dump(mode="json") if hasattr(run.cfg, "model_dump") else {}
    snapshot = {"schema_version": 1, "baseline": request.build_base if restored else tickets._git(run.repo_root, "rev-parse", "HEAD"),
                "work_item": item.model_dump(), "config_sha256": tickets._digest(config)}
    if snapshot["baseline"] != request.build_base:
        raise tickets.TicketError("delivery baseline changed before input binding")
    binding = run.session_dir / "work_item.json"
    if binding.exists() and json.loads(binding.read_text()) != item.model_dump():
        raise tickets.TicketError("session work_item changed; use a new adw_id")
    if saved.exists() and json.loads(saved.read_text()) != snapshot:
        raise tickets.TicketError("immutable delivery input changed; use a new adw_id")
    tickets.atomic_json(binding, item.model_dump())
    tickets.atomic_json(saved, snapshot)


def verify_frozen_input(run, request):
    snapshot = json.loads((run.session_dir / "delivery-input.json").read_text())
    config = run.cfg.model_dump(mode="json") if hasattr(run.cfg, "model_dump") else {}
    if (snapshot["work_item"] != request.work_item.model_dump()
            or snapshot["baseline"] != request.build_base
            or snapshot["config_sha256"] != tickets._digest(config)):
        raise tickets.TicketError("frozen delivery inputs changed before build")
    tickets.validate_work_item(run, request.work_item, check_tree=False)
    restored = getattr(run, "delivery_recovery", None)
    if restored:
        if (restored.work_item != request.work_item or restored.build_base != request.build_base
                or restored.workspace != recovery.workspace(run)):
            raise tickets.TicketError("recovery input or workspace changed before build")
        return
    if tickets._git(run.repo_root, "rev-parse", "HEAD") != request.build_base:
        raise tickets.TicketError("delivery baseline changed before build")
    if isinstance(request.work_item, TicketWorkItem):
        tickets._assert_clean_baseline(run)


def launch(run, target):
    """Expected input rejection is distinct from a business/runtime failure."""
    if target.resume or target.retry:
        return recover(run, target)
    rejected = None
    with run.phase(PhaseParams(name="delivery_input", kind="code", owner="delivery",
                               description="Resolve prerequisites and freeze clean delivery inputs before agents")) as ph:
        try:
            required = quality.required_checks()
            # Ticket resolution and proof checks precede baseline validation.
            if target.ticket:
                item = resolve_input(run, target)
                preflight(run)
            else:
                preflight(run)
                item = resolve_input(run, target)
            request = DeliveryRequest(target.prompt or "Deliver the bound work item and all its acceptance obligations.",
                                      item, tickets._git(run.repo_root, "rev-parse", "HEAD"))
            freeze_input(run, request)
            ph.log(work_item=item.model_dump(), mandatory_checks=required)
        except ValueError as error:
            rejected = {"status": "preflight_rejected", "reason": str(error)}
            tickets.atomic_json(run.session_dir / "preflight-result.json", rejected)
            ph.log(**rejected)
    if rejected:
        run.finish(accepted=False, reason="preflight_rejected: " + rejected["reason"])
        return 2
    return execute(run, request)


def recover(run, target: BuildInput) -> int:
    rejected = None
    with run.phase(PhaseParams(name="recovery_input", kind="code", owner="delivery",
                               description="Validate the stopped attempt and exact retained workspace before recovery")) as ph:
        try:
            state = recovery.load(run, target.resume or target.retry, "resume" if target.resume else "retry")
            quality.required_checks()
            ph.log(source=state.source, mode=state.mode, build_completed=state.build is not None)
        except ValueError as error:
            rejected = {"status": "preflight_rejected", "reason": str(error)}
            tickets.atomic_json(run.session_dir / "preflight-result.json", rejected)
            ph.log(**rejected)
    if rejected:
        run.finish(accepted=False, reason="preflight_rejected: " + rejected["reason"])
        return 2
    run.delivery_recovery = state
    return execute(run, DeliveryRequest(state.prompt, state.work_item, state.build_base))


def capture(run, base, notes):
    changeset = changes.capture(run, ChangeCapture(base=base))
    return changes.as_envelope(changeset, notes)


def execute(run, request: DeliveryRequest) -> int:
    freeze_input(run, request)
    spec_artifacts._layout(run, request.work_item.spec.path)
    verify_frozen_input(run, request)
    state = getattr(run, "delivery_recovery", None) or recovery.start(run, request)
    recovery.save(run, state)
    run.active_delivery_checkpoint = state
    try:
        result = _execute(run, request, state)
    except BaseException as error:
        if not getattr(run, "delivery_stop_saved", False):
            try:
                recovery.stop(run, state, error)
            except Exception as checkpoint_error:
                run.console.note(f"恢复检查点保存失败，不能安全继续：{checkpoint_error}")
        if hasattr(run.tracer, "session_finish"):
            run.tracer.session_finish(run.adw_id, ok=False)
        if hasattr(run, "close"):
            run.close()
        raise
    finally:
        run.active_delivery_checkpoint = None
    state.status = "completed" if result == 0 else "stopped"
    state.error = "" if result == 0 else (getattr(run, "reason", "") or "delivery was not accepted or publication failed")
    if result and hasattr(run.tracer, "session_finish"):
        run.tracer.session_finish(run.adw_id, ok=False)
    recovery.save(run, state)
    return result


def _execute(run, request: DeliveryRequest, state: recovery.DeliveryCheckpoint) -> int:
    required = sorted(set(quality.required_checks()) | set(state.required_checks))
    tickets.bind_work_item(run, AgentCall(output_type=BuildOutput, prompt=request.prompt,
                                         work_item=request.work_item), "builder")
    build = state.build
    if build is None:
        recovery.save(run, state)
        prompt = request.prompt
        if state.source:
            prompt += (f"\nRecovery from {state.source} ({state.mode}). Inspect and retain the existing partial work; "
                       f"complete the same target. Prior failure: {state.source_error}")
        with run.phase(PhaseParams(name="build", kind="agent", owner="builder", retries=1,
                                   description="Implement the selected target, retaining any prior partial implementation")) as ph:
            build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt,
                work_item=request.work_item, previous=state.previous_review or request.previous))
        state.build = build
        recovery.save(run, state)
    else:
        with run.phase(PhaseParams(name="restore_build", kind="code", owner="delivery",
                                   description="Reuse completed implementation while requiring fresh verification and review")) as ph:
            ph.log(source=state.source, summary=build.summary)
    repairs, verifications, round_number = state.repairs, state.verifications, 0
    previous_review = state.previous_review
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
                                   + "\nQuality applicability: " + str(quality.not_applicable_checks()))
            if previous_review:
                review_input.notes_for_next_agent += "\nRetain and reassess all prior obligations:\n" + previous_review.model_dump_json()
            ph.log(diff=review_input.diff_path)
        with run.phase(PhaseParams(name=f"review_{round_number}", kind="agent", owner="reviewer", retries=1,
                                   description="Judge target requirements and manual obligations against current checks")) as ph:
            review = ph.call(AgentCall(output_type=ReviewOutput, prompt=request.prompt,
                work_item=request.work_item, previous=review_input,
                gates=[gates.artifacts_exist, gates.verdict_consistent, gates.obligations_retained(previous_review)]
                      + ([gates.verification_artifacts] if isinstance(request.work_item, TicketWorkItem) else [])))
        previous_review = review
        state.previous_review = review
        state.required_checks = sorted(set(required) | set(results))
        recovery.save(run, state)
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
            state.repairs = repairs
            # A partial repair invalidates the previous successful builder output.
            state.build = None
            state.required_checks = sorted(set(state.required_checks) | set(decision.checks))
            recovery.save(run, state)
            with run.phase(PhaseParams(name=f"revise_{repairs}", kind="agent", owner="builder", retries=1,
                                       description="Repair owned implementation defects while retaining the same target")) as ph:
                build = ph.call(AgentCall(output_type=BuildOutput, prompt=request.prompt,
                                         work_item=request.work_item, previous=review))
            state.build = build
            recovery.save(run, state)
            pending = sorted(set(required) | set(results) | set(decision.checks))
            results = {}
        else:
            verifications += 1
            state.verifications = verifications
            state.required_checks = sorted(set(state.required_checks) | set(decision.checks))
            recovery.save(run, state)
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
            manual.extend(tickets.verification_artifact(run.repo_root, value) for value in obligation.evidence)
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
        run.reason = f"delivery acceptance publication failed: {error}"
        run.console.note(f"交付验收签发失败，需使用 adw-recheck 重新验证：{error}")
        return 1
    return 0
