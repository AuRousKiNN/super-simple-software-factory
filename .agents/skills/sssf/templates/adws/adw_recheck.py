#!/usr/bin/env -S uv run
# /// script
# dependencies = ["openai-codex==0.155.1", "pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Recheck — validate new evidence, run configured checks, review the same target.

Usage:
    uv run adws/adw_recheck.py path/to/recheck.json [--config adws/adw_sssf_config/sssf.config.yaml]

Phases: code(recheck_input) -> [code(checks)] -> code(changes) -> reviewer -> code(route)
        -> [code(checks) -> code(changes) -> reviewer -> code(route)] bounded

Always creates a new session. Does not invoke a builder, commit, or issue ticket
acceptance records; a custom acceptance ADW must establish its complete obligations.
"""
import argparse
import sys
from pathlib import Path

from adw_modules import agents, changes, gates, quality, review_routing, session
from adw_modules.data_types import AgentCall, ChangeCapture, PhaseParams, RecheckRequest, ReviewOutput

REQUIRED_AGENTS = ["reviewer"]
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
                                            + "\n" + review_routing.evidence_notes(results))
            ph.log(diff=changeset.diff_path)
        with run.phase(PhaseParams(name=f"review_{round_number}", kind="agent", owner="reviewer", retries=1,
                                   description="Reassess all assigned obligations using the newly supplied evidence")) as ph:
            review = ph.call(AgentCall(output_type=ReviewOutput, prompt=prepared.source.prompt,
                                       work_item=prepared.source.work_item, previous=previous,
                                       gates=[gates.artifacts_exist, gates.verdict_consistent,
                                              gates.obligations_retained(previous_review)]))
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
            return run.finish(accepted=decision.action == "approve", reason=decision.reason)
        pending = decision.checks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", help="repository-local JSON matching RecheckRequest")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    args = parser.parse_args()
    sys.exit(main(args.request, args.config))
