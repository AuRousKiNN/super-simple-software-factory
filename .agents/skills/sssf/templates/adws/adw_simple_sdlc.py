#!/usr/bin/env -S uv run
# /// script
# dependencies = ["openai-codex==0.155.1", "pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""ADW Simple SDLC — plan, build, test, review, document, committing as it goes.

Usage:
    uv run adws/adw_simple_sdlc.py "<prompt or path/to/prompt.md>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]

Phases: engineer(request) -> planner -> git(commit_plan)
        -> builder -> code(test) -> code(changes) -> reviewer -> code(route)
        -> [builder(revise) -> code(test) | code(verify) -> reviewer -> code(route)] bounded
        -> git(commit_build) -> code(changes) -> documenter -> git(commit_docs)

Three commits, three work products, three authors. The plan, the code, and the
write-up each land in their own commit, and each commit message is the words of
the agent that produced it — `commit_message` on PlanOutput describes the spec,
on BuildOutput the code, on DocumentOutput the write-up. No agent's sentence is
ever reused for another agent's diff.

Testing is CODE, not an agent. `bun test` is a command, not a judgement call:
an agent rediscovering it every run costs a million tokens to learn what a
subprocess already knows. Actual evidence reaches the reviewer, which classifies
implementation defects, missing checks and external conditions for Python routing.

Two different questions still get asked, in order. The suite asks "does it
run"; the reviewer asks "is this what was asked for", against `plan.md` — and
neither can answer the other's. A revision that closes a review finding
re-enters the suite, so the tree that gets committed is the tree that was both
tested and approved.

The code commit lands after verification, not straight after the build: fixes
and revisions are part of the same work product, and red code has no business
on the branch. A run that fails verification therefore leaves the plan
committed and the working tree dirty — the spec is a real artifact either way,
and the unfinished code stays where the engineer can see it.

The documenter measures against the commit this run STARTED from, not against
`main`, because by then the run has moved `main` itself. That baseline is
pinned before the first commit phase and printed in the request phase.
"""

import argparse
import sys

from adw_modules import agents, changes, gates, git_helper, quality, review_routing, session, tickets, utils
from adw_modules.data_types import (AgentCall, BuildOutput, ChangeCapture,
                                    DocumentOutput, PhaseParams, PlanOutput,
                                    ReviewOutput)

REQUIRED_AGENTS = ["planner", "builder", "reviewer", "documenter"]
MAX_REVISION_LOOPS = 3
MAX_VERIFICATION_LOOPS = 2

DOCUMENT_NOTES = ("Read diff_path in full before writing. Document only what the "
                  "diff shows, then copy the write-up into app_docs/ as your task "
                  "describes.")


def main(prompt: str, config: str = "adws/adw_sssf_config/sssf.config.yaml", adw_id: str | None = None) -> int:
    cfg = agents.load_config(config)
    agents.validate(cfg, REQUIRED_AGENTS)
    run = session.ensure(cfg, adw_id)
    baseline = git_helper.rev("HEAD")     # pinned before this run commits anything

    def commit(ph, envelope) -> str:
        """Commit what the preceding phase produced, in that agent's own words."""
        message = envelope.commit_message or f"sssf({run.adw_id}): {envelope.summary}"
        sha = git_helper.commit_all(message)
        ph.log(sha=sha, message=message)
        return sha

    def capture_changes(ph, base: str, notes: str):
        """Record Git's complete tracked-plus-untracked view for an agent handoff."""
        changeset = changes.capture(run, ChangeCapture(base=base))
        ph.log(base=f"{changeset.base.label} @ {changeset.base.commit[:7]}",
               reason=changeset.base.reason,
               files=len(changeset.files) + len(changeset.untracked),
               lines=f"+{changeset.insertions} -{changeset.deletions}",
               diff=changeset.diff_path)
        if changeset.empty:
            raise RuntimeError(
                f"nothing changed since {changeset.base.label} "
                f"({changeset.base.reason})")
        return changes.as_envelope(changeset, notes)

    def record(ph, result) -> None:
        """Log a deterministic block's verdict — the same shape every ADW uses."""
        passed = sum(1 for check in result.checks if check.passed)
        ph.log(passed=result.passed, checks=f"{passed}/{len(result.checks)}",
               artifacts=", ".join(result.artifacts))

    with run.phase(PhaseParams(name="request", kind="engineer", owner=run.engineer,
                               description="Capture the incoming ask")) as ph:
        ph.log(input=prompt, baseline=git_helper.short_sha(baseline))

    with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                               description="Turn the request into an implementable plan")) as ph:
        plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                                 gates=[gates.artifacts_exist, gates.files_non_empty]))

    with run.phase(PhaseParams(name="commit_plan", kind="code", owner="git",
                               description="Put the spec on record before any code exists to blur it")) as ph:
        build_base = commit(ph, plan)

    with run.phase(PhaseParams(name="spec_input", kind="code", owner="tickets",
                               description="Bind the current root spec for implementation and all repairs")) as ph:
        work_item = tickets.spec_work_item(run.repo_root, plan.spec_path)
        ph.log(work_item=work_item.model_dump())

    with run.phase(PhaseParams(name="build", kind="agent", owner="builder",
                               description="Implement the plan exactly")) as ph:
        build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, work_item=work_item, previous=plan))

    repairs = verifications = round_number = 0
    previous_review = None
    results = {}
    needs_test = True
    while True:
        round_number += 1
        if needs_test:
            with run.phase(PhaseParams(name=f"test_{round_number}", kind="code", owner="quality",
                                       description="Run required tests against the latest builder output")) as ph:
                test = quality.run_tests(run)
                record(ph, test)
                results.update({c.name: c for c in test.checks})
            needs_test = False
        with run.phase(PhaseParams(name=f"changes_review_{round_number}", kind="code", owner="git",
                                   description="Capture the current implementation and verification evidence")) as ph:
            results = review_routing.refresh_results(run, results)
            review_input = capture_changes(ph, build_base, review_routing.evidence_notes(results))
        with run.phase(PhaseParams(name=f"review_{round_number}", kind="agent", owner="reviewer", retries=1,
                                   description="Judge the bound target and classify defects or missing proof")) as ph:
            review = ph.call(AgentCall(output_type=ReviewOutput, prompt=prompt, work_item=work_item, previous=review_input,
                                       gates=[gates.artifacts_exist, gates.verdict_consistent,
                                              gates.obligations_retained(previous_review)]))
        previous_review = review
        with run.phase(PhaseParams(name=f"route_{round_number}", kind="code", owner="review_routing",
                                   description="Save complete review ownership and enforce bounded routing")) as ph:
            results = review_routing.refresh_results(run, results)
            decision = review_routing.acceptance_decision(review, review_routing.RoutingPolicy(
                repair_remaining=MAX_REVISION_LOOPS - repairs,
                verification_remaining=MAX_VERIFICATION_LOOPS - verifications,
                checks=quality.configured_checks()), results, ["test"])
            if decision.action == "repair" and (set(results) - {"test"}) and verifications >= MAX_VERIFICATION_LOOPS:
                decision = decision.model_copy(update={"action": "handoff", "reason": "verification budget exhausted; repair would invalidate existing checks"})
            receipt = review_routing.save(run, review, decision,
                review_routing.ReceiptContext(prompt, build_base, work_item, sorted({"test", *results})))
            ph.log(receipt=str(receipt), **decision.model_dump())
        if decision.action == "handoff":
            return run.finish(accepted=False, reason=decision.reason)
        if decision.action == "approve":
            break
        if decision.action == "repair":
            repairs += 1
            with run.phase(PhaseParams(name=f"revise_{repairs}", kind="agent", owner="builder", retries=1,
                                       description="Repair implementation or test defects within the bound target")) as ph:
                build = ph.call(AgentCall(output_type=BuildOutput, prompt=prompt, work_item=work_item, previous=review))
            pending = sorted((set(results) | set(decision.checks)) - {"test"})
            results = {}
            needs_test = True
        else:
            pending = decision.checks
        if pending:
            verifications += 1
            with run.phase(PhaseParams(name=f"verify_{verifications}", kind="code", owner="quality",
                                       description="Execute the requested configured checks before re-review")) as ph:
                result = quality.run_selected(run, pending)
                results.update({c.name: c for c in result.checks})
                record(ph, result)

    with run.phase(PhaseParams(name="commit_build", kind="code", owner="git",
                               description="Land the code only now: green suite, approved review")) as ph:
        commit(ph, build)

    with run.phase(PhaseParams(name="changes", kind="code", owner="git",
                               description="Diff the whole run against its pinned baseline, for the documenter")) as ph:
        document_input = capture_changes(ph, baseline, DOCUMENT_NOTES)

    with run.phase(PhaseParams(name="document", kind="agent", owner="documenter", retries=1,
                               description="Write up the completed change")) as ph:
        document = ph.call(AgentCall(output_type=DocumentOutput, prompt=prompt,
                                     previous=document_input,
                                     gates=[gates.artifacts_exist, gates.files_non_empty]))

    with run.phase(PhaseParams(name="commit_docs", kind="code", owner="git",
                               description="Ship the write-up in its own commit, beside the code it describes")) as ph:
        commit(ph, document)

    return run.finish(accepted=True, reason="review and current mandatory checks passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="inline text or a path to a prompt file")
    parser.add_argument("--config", default="adws/adw_sssf_config/sssf.config.yaml")
    parser.add_argument("--adw-id", default=None, help="join or pin an existing session")
    args = parser.parse_args()
    sys.exit(main(utils.resolve_prompt(args.prompt), args.config, args.adw_id))
