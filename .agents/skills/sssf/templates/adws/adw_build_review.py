#!/usr/bin/env -S uv run
# /// script
# dependencies = ["openai-codex==0.155.1", "pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Build Review — implement, then confirm it is what was asked for.

Usage:
    uv run adws/adw_build_review.py "<prompt or path/to/prompt.md>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]

Phases: engineer(request) -> builder -> code(changes) -> reviewer -> code(route)
        -> [builder(revise) | code(verify) -> code(changes) -> reviewer -> code(route)] bounded

Review is not testing. Tests answer "does it run"; the reviewer answers "is this
the thing that was asked for" — it reads the spec (`plan.md` from a prior plan
phase if the session has one, else the prompt verbatim), reads the code that was
written, and rules on each requirement.

Like the tester, the reviewer's phase succeeds when it RUNS and REPORTS. A
rejection does not fail the phase; it fails the run, checked at the end, after
the bounded revise loop has had its chances.
"""

import argparse
import sys

from adw_modules import agents, changes, gates, git_helper, quality, review_routing, session, utils
from adw_modules.data_types import (AgentCall, BuildOutput, ChangeCapture,
                                    PhaseParams, ReviewOutput)

REQUIRED_AGENTS = ["builder", "reviewer"]
MAX_REVISION_LOOPS = 3
MAX_VERIFICATION_LOOPS = 2


def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, adw_id)
    build_base = git_helper.rev("HEAD")

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input=prompt, build_base=git_helper.short_sha(build_base))

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder",
                               description="Implement the request")) as ph:
        ph.call(AgentCall(output_type=BuildOutput, prompt=prompt))

    repairs = verifications = round_number = 0
    previous_review = None
    results = {}
    while True:
        round_number += 1
        with run.phase(PhaseParams(name=f"changes_{round_number}", kind="code", owner="git",
                                   description="Capture the current implementation for independent review")) as ph:
            results = review_routing.refresh_results(run, results)
            changeset = changes.capture(run, ChangeCapture(base=build_base))
            ph.log(diff=changeset.diff_path)
            review_input = changes.as_envelope(changeset, review_routing.evidence_notes(results))

        with run.phase(PhaseParams(name=f"review_{round_number}", kind="agent", owner="reviewer", retries=1,
                                   description="Judge requirements and classify remaining problems by owner")) as ph:
            review = ph.call(AgentCall(output_type=ReviewOutput, prompt=prompt, previous=review_input,
                                       gates=[gates.artifacts_exist, gates.verdict_consistent,
                                              gates.obligations_retained(previous_review)]))
        previous_review = review

        with run.phase(PhaseParams(name=f"route_{round_number}", kind="code", owner="review_routing",
                                   description="Persist the review and choose an available bounded action")) as ph:
            results = review_routing.refresh_results(run, results)
            decision = review_routing.acceptance_decision(review, review_routing.RoutingPolicy(
                repair_remaining=MAX_REVISION_LOOPS - repairs,
                verification_remaining=MAX_VERIFICATION_LOOPS - verifications,
                checks=quality.configured_checks()), results, [])
            if decision.action == "repair" and results and verifications >= MAX_VERIFICATION_LOOPS:
                decision = decision.model_copy(update={"action": "handoff", "reason": "verification budget exhausted; repair would invalidate existing checks"})
            receipt = review_routing.save(run, review, decision,
                                         review_routing.ReceiptContext(prompt, build_base, mandatory_checks=sorted(results)))
            ph.log(receipt=str(receipt), **decision.model_dump())
        if decision.action in {"approve", "handoff"}:
            return run.finish(accepted=decision.action == "approve", reason=decision.reason)
        if decision.action == "repair":
            repairs += 1
            with run.phase(PhaseParams(name=f"revise_{repairs}", kind="agent", owner="builder", retries=1,
                                       description="Repair only current-target implementation or test defects")) as ph:
                ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, previous=review))
            # Every executed check must be renewed after implementation changes.
            pending = sorted(set(results) | set(decision.checks))
            results = {}
        else:
            pending = decision.checks
        if pending:
            verifications += 1
            with run.phase(PhaseParams(name=f"verify_{verifications}", kind="code", owner="quality",
                                       description="Execute configured checks and hand actual evidence to review")) as ph:
                result = quality.run_selected(run, pending)
                results.update({c.name: c for c in result.checks})
                ph.log(passed=result.passed, artifacts=result.artifacts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="inline text or a path to a prompt file")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), args.config, args.adw_id))
