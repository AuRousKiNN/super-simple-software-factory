#!/usr/bin/env -S uv run
# /// script
# dependencies = ["openai-codex==0.155.1", "pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Recheck — validate new evidence, run configured checks, review the same target.

Usage:
    uv run adws/adw-recheck.py path/to/recheck.json [--config adws/adw_sssf_config/sssf.config.yaml]

Phases: code(recheck_input) -> scout(evidence freshness) -> [code(checks)] -> code(changes) -> reviewer -> code(route)
        -> [code(checks) -> code(changes) -> reviewer -> code(route)] bounded

Always creates a new session without a builder. Ticket mode reruns required checks,
reviews all obligations, publishes specification documentation and issues current-HEAD acceptance.
Successful ticket rechecks commit documentation only; implementation content stays unchanged.
"""
import argparse
import sys
from pathlib import Path

from adw_modules import delivery, agents, changes, evidence_freshness, gates, quality, review_routing, session, spec_artifacts, tickets
from adw_modules.data_types import TicketWorkItem, AgentCall, ChangeCapture, DocumentRequest, FinishOptions, PhaseParams, RecheckRequest, ReviewOutput

REQUIRED_AGENTS = ["scout", "reviewer", "documenter"]
MAX_VERIFICATION_LOOPS = 2


def main(request_path: str, config: str = "adws/adw_sssf_config/sssf.config.yaml") -> int:
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, None)
    with run.phase(PhaseParams(name="recheck_input", kind="code", owner="review_routing",
                               description="Validate the original target, current baseline and new evidence before review")) as ph:
        request = RecheckRequest.model_validate_json(Path(request_path).read_text())
        prepared = review_routing.prepare_recheck(run, request, quality.configured_checks(), Path(request_path))
        ph.log(original_review=request.original_review.model_dump(), baseline=request.baseline,
               changed=prepared.changed, checks=prepared.checks)

    freshness = evidence_freshness.inspect(run,
        [request.original_review, *evidence_freshness.dependency_refs(prepared.source.work_item),
         *(e.artifact for e in request.evidence)], prepared.source.work_item)
    reason = evidence_freshness.failure_reason(freshness)
    if reason:
        return run.finish(accepted=False, reason=reason)

    previous_review = prepared.source.review
    results = {}
    pending = prepared.checks
    verifications = round_number = 0
    while True:
        round_number += 1
        if pending:
            verifications += 1
            with run.phase(PhaseParams(name=f"verify_{verifications}", kind="code", owner="quality",
                                       description="Produce current execution evidence from configured checks")) as ph:
                result = quality.run_selected(run, pending)
                results.update({c.name: c for c in result.checks})
                ph.log(passed=result.passed, artifacts=result.artifacts)
        with run.phase(PhaseParams(name=f"changes_{round_number}", kind="code", owner="git",
                                   description="Capture current code and preserve the original review and supplied proof")) as ph:
            # Check hashes again before presenting evidence, including after code phases.
            for evidence in request.evidence:
                review_routing.tickets.verify_ref(run.repo_root, evidence.artifact)
            results = review_routing.refresh_results(run, results)
            changeset = changes.capture(run, ChangeCapture(base=prepared.source.build_base))
            previous = changes.as_envelope(changeset, review_routing.recheck_notes(prepared)
                                            + "\n" + review_routing.evidence_notes(results)
                                            + "\n" + evidence_freshness.notes(freshness))
            ph.log(diff=changeset.diff_path)
        with run.phase(PhaseParams(name=f"review_{round_number}", kind="agent", owner="reviewer", retries=1,
                                   description="Reassess all assigned obligations using the newly supplied evidence")) as ph:
            review = ph.call(AgentCall(output_type=ReviewOutput, prompt=prepared.source.prompt,
                                       work_item=prepared.source.work_item, previous=previous,
                                       gates=[gates.artifacts_exist, gates.verdict_consistent,
                                              gates.obligations_retained(previous_review)]
                                       + ([gates.verification_artifacts] if isinstance(prepared.source.work_item, TicketWorkItem) else [])))
        previous_review = review
        with run.phase(PhaseParams(name=f"route_{round_number}", kind="code", owner="review_routing",
                                   description="Save the new verdict and stop unless bounded verification can close it")) as ph:
            for evidence in request.evidence:
                review_routing.tickets.verify_ref(run.repo_root, evidence.artifact)
            results = review_routing.refresh_results(run, results)
            decision = review_routing.acceptance_decision(review, review_routing.RoutingPolicy(
                verification_remaining=MAX_VERIFICATION_LOOPS - verifications,
                checks=quality.configured_checks()), results, prepared.checks)
            receipt = review_routing.save(run, review, decision, review_routing.ReceiptContext(
                prepared.source.prompt, prepared.source.build_base, prepared.source.work_item,
                sorted(set(prepared.checks) | set(results))))
            ph.log(receipt=str(receipt), **decision.model_dump())
        if decision.action != "verify":
            if isinstance(prepared.source.work_item, TicketWorkItem):
                return delivery.complete(run, delivery.DeliveryEvidence(
                    delivery.DeliveryRequest(prepared.source.prompt, prepared.source.work_item, prepared.source.build_base),
                    review, receipt, results, prepared.checks, decision,
                    "记录票据重验与当前基线验收", tuple(e.artifact for e in request.evidence), recheck=True))
            document = spec_artifacts.document(run, DocumentRequest(
                work_item=prepared.source.work_item, purpose="记录补证与复核结果。" + decision.reason,
                changes=previous, checks=list(results.values()), review=review,
                review_receipt=tickets.artifact(run.repo_root, spec_artifacts.relative(run, receipt), ".json"),
                evidence=[request.original_review, *[e.artifact for e in request.evidence],
                    tickets.artifact(run.repo_root, spec_artifacts.relative(run, receipt), ".json")]))
            spec_artifacts.prepare_finish(run, document, FinishOptions(
                receipt=tickets.artifact(run.repo_root, spec_artifacts.relative(run, receipt), ".json"),
                accepts_scope=True))
            return run.finish(accepted=decision.action == "approve", reason=decision.reason)
        pending = decision.checks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="repository-local JSON matching RecheckRequest")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    args = parser.parse_args()
    sys.exit(main(args.request, args.config))
