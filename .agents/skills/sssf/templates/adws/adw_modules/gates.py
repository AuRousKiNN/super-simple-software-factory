"""Validation gates: verify the envelope's CLAIMS, never guesses.

A gate is `gate(envelope, run) -> GateReport` — one check per item it looked at.
Violations are derived from the failed checks and sent back to the SAME agent
session as a correction. Every check is recorded either way, so a green gate
says WHAT it verified instead of only that it passed.

Gates check what is mechanically checkable; plan quality is a reviewer's job.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .data_types import EnvelopeBase, GateReport

TAIL_CHARS = 1000        # command output kept as evidence on a failure


def _size(path: Path) -> str:
    n = path.stat().st_size
    return f"{n}B" if n < 1024 else f"{n / 1024:.1f}KB"


def artifacts_exist(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        report.check(a, p.exists(),
                     f"exists, {_size(p)}" if p.exists() else "declared artifact does not exist")
    return report


def files_non_empty(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        if not (p.exists() and p.is_file()):
            continue                       # existence is artifacts_exist's job
        empty = p.stat().st_size == 0
        report.check(a, not empty, "declared artifact is empty" if empty else _size(p))
    return report


def json_parses(envelope: EnvelopeBase, run) -> GateReport:
    report = GateReport()
    for a in envelope.artifacts:
        p = Path(a)
        if p.suffix != ".json" or not p.exists():
            continue
        try:
            parsed = json.loads(p.read_text())
            report.check(a, True, f"parses, {type(parsed).__name__}")
        except json.JSONDecodeError as e:
            report.check(a, False, f"declared JSON artifact does not parse: {e}")
    return report


def verdict_consistent(envelope: EnvelopeBase, run) -> GateReport:
    """A review's verdict must agree with the findings it just wrote down.

    Nothing here judges the code — that is the reviewer's job. This checks the
    envelope against itself: an approval that ships blocking items, or a
    rejection that names no problem, is a claim the harness can refute without
    reading a line of the diff.
    """
    from .review_routing import BLOCKER_OWNERS
    from .data_types import ReviewOutput

    report = GateReport()
    if not isinstance(envelope, ReviewOutput):
        return report.check("review type", False, "verdict gate requires ReviewOutput")
    review = envelope
    report.check("review completed", review.status == "success", "business verdicts require status=success")
    report.check("requirements present", bool(review.findings), "review must judge its target requirements")
    report.check("approved vs blocking", not (review.approved and review.blocking), "approval requires no blockers")
    report.check("rejection names a problem", review.approved or bool(review.blocking),
                 "rejection requires structured blockers for deterministic routing")
    ids = [b.id for b in review.blocking]
    report.check("unique blocker ids", len(ids) == len(set(ids)), "blocker IDs must be unique")
    covered = {basis for b in review.blocking for basis in b.basis}
    for finding in review.findings:
        report.check(f"finding {finding.requirement}", bool(finding.requirement.strip() and finding.evidence.strip()),
                     "requirement and evidence must be explicit")
        report.check(f"unmet {finding.requirement}", finding.met or (not review.approved and finding.requirement in covered),
                     "unmet findings must be covered verbatim in a blocker's basis")
    obligation_ids = [o.id for o in review.required_verification]
    report.check("unique obligation ids", len(obligation_ids) == len(set(obligation_ids)), "obligation IDs must be unique")
    for obligation in review.required_verification:
        report.check(f"obligation {obligation.id}", bool(obligation.id.strip() and obligation.description.strip()),
                     "mandatory obligations need an identity and description")
        report.check(f"proof {obligation.id}", not obligation.satisfied or bool(obligation.evidence) and all(e.strip() for e in obligation.evidence),
                     "satisfied obligations need evidence")
        report.check(f"pending {obligation.id}", obligation.satisfied or (not review.approved and obligation.id in covered),
                     "pending mandatory obligations must block approval and name a blocker")
    for blocker in review.blocking:
        required = [blocker.id, blocker.description, blocker.trigger, blocker.consequence, blocker.closure, blocker.handoff]
        report.check(f"blocker {blocker.id} detail", all(v.strip() for v in required)
                     and bool(blocker.basis) and all(v.strip() for v in blocker.basis)
                     and bool(blocker.evidence) and all(v.strip() for v in blocker.evidence),
                     "basis, trigger, impact, evidence, closure and handoff are required")
        report.check(f"blocker {blocker.id} owner", blocker.owner == BLOCKER_OWNERS[blocker.kind],
                     "owner must match the structured problem kind")
        report.check(f"blocker {blocker.id} checks", (bool(blocker.checks) and all(c.strip() for c in blocker.checks))
                     if blocker.kind == "check_execution" else not blocker.checks,
                     "only check_execution names configured check IDs; at least one is required")
        if blocker.kind == "manual_validation":
            report.check(f"blocker {blocker.id} manual", bool(blocker.preconditions.strip() and blocker.pass_criteria.strip())
                         and bool(blocker.steps) and all(s.strip() for s in blocker.steps),
                         "required manual validation needs preconditions, steps and pass criteria")
        report.check(f"blocker {blocker.id} integration", not blocker.invalidated_evidence or bool(blocker.affected_tickets),
                     "invalidated integration evidence must identify affected tickets")
    return report


def verification_artifacts(envelope: EnvelopeBase, run) -> GateReport:
    """Return invalid ticket proof to the same reviewer within its repair budget."""
    from . import tickets

    report = GateReport()
    for obligation in envelope.required_verification:
        if not obligation.satisfied:
            continue
        report.check(f"proof {obligation.id}", bool(obligation.evidence),
                     "satisfied obligations need retained file evidence")
        for value in obligation.evidence:
            try:
                tickets.verification_artifact(run.repo_root, value)
            except (ValueError, OSError, RuntimeError) as error:
                report.check(f"proof {obligation.id}: {value}", False,
                    f"{error}. Cite a stable repository file or the current review report. "
                    "Keep SDK/dependency references in report prose; record the version, "
                    "inspected contract and applicable findings there. Do not drop the obligation "
                    "or claim missing validation passed.")
            else:
                report.check(f"proof {obligation.id}: {value}", True, "retained file evidence")
    return report


def obligations_retained(previous):
    """Keep the same review target and mandatory obligations across repair/recheck."""
    def gate(envelope, run):
        report = GateReport()
        if previous is None:
            return report
        requirements = {f.requirement for f in envelope.findings}
        obligations = {o.id for o in envelope.required_verification}
        for finding in previous.findings:
            report.check(f"retained {finding.requirement}", finding.requirement in requirements,
                         "the review target cannot silently lose a requirement")
        for obligation in previous.required_verification:
            report.check(f"retained {obligation.id}", obligation.id in obligations,
                         "an existing mandatory obligation must be re-evaluated, not omitted")
        return report
    gate.__name__ = "obligations_retained"
    return gate


def tests_pass(command: str):
    """Gate factory: the given shell command must exit 0."""
    def gate(envelope: EnvelopeBase, run) -> GateReport:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        ok = result.returncode == 0
        note = f"exit {result.returncode}"
        if not ok:
            note += "\n" + (result.stdout + result.stderr)[-TAIL_CHARS:]
        return GateReport().check(command, ok, note)
    gate.__name__ = f"tests_pass({command})"
    return gate
