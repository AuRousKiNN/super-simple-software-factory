"""Deterministic review ownership, bounded action selection and durable handoffs.

ADWs own sequencing and acceptance. This module never dispatches an agent or
executes a reviewer-supplied command. Receipts are host-owned, immutable per phase.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import gates, tickets
from .data_types import (AgentCall, RecheckRequest, ReviewDecision, ReviewOutput,
                         ReviewReceipt, SpecWorkItem, TicketWorkItem)

BLOCKER_OWNERS = {
    "implementation": "builder", "test_implementation": "builder",
    "check_execution": "quality", "spec_conflict": "planner",
    "ticket_conflict": "decomposer", "environment": "environment",
    "manual_validation": "human", "external_regression": "external",
    "protocol_issue": "external",
}


@dataclass
class RoutingPolicy:
    repair_remaining: int = 0
    verification_remaining: int = 0
    checks: set[str] = field(default_factory=set)


def decide(review: ReviewOutput, policy: RoutingPolicy) -> ReviewDecision:
    report = gates.verdict_consistent(review, None)
    if not report.passed:
        raise ValueError("inconsistent review: " + "; ".join(report.violations))
    if review.approved:
        return ReviewDecision(action="approve", reason="review approved all assigned obligations")
    owners = list(dict.fromkeys(b.owner for b in review.blocking))
    detail = "; ".join(f"{b.id} [{b.kind}/{b.owner}]: {b.description}; closure: {b.closure}" for b in review.blocking)
    kinds = {b.kind for b in review.blocking}
    if "spec_conflict" in kinds:
        owners = ["planner", *[o for o in owners if o != "planner"]]
        return ReviewDecision(action="handoff", owners=owners,
                              reason="root contract decision first; use new planning and implementation sessions: " + detail)
    if kinds - {"implementation", "test_implementation", "check_execution"}:
        return ReviewDecision(action="handoff", owners=owners, reason="responsible owner must close blockers: " + detail)
    checks = sorted({c for b in review.blocking for c in b.checks})
    missing = sorted(set(checks) - policy.checks)
    if missing:
        return ReviewDecision(action="handoff", owners=owners, checks=checks,
                              reason=f"workflow capability missing for checks {missing}; configure quality entry points: " + detail)
    needs_repair = bool(kinds & {"implementation", "test_implementation"})
    # Mixed automatic work is admitted only when every requested action is available.
    if needs_repair and policy.repair_remaining <= 0:
        return ReviewDecision(action="handoff", owners=owners, checks=checks,
                              reason="builder repair budget exhausted or unavailable: " + detail)
    if checks and policy.verification_remaining <= 0:
        return ReviewDecision(action="handoff", owners=owners, checks=checks,
                              reason="verification budget exhausted or unavailable: " + detail)
    return ReviewDecision(action="repair" if needs_repair else "verify", owners=owners,
                          checks=checks, reason=detail)


def _git(run, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=run.repo_root, capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace"))
    return result.stdout


def baseline(run) -> str:
    return _git(run, "rev-parse", "HEAD").decode().strip()


def tree_files(run) -> dict[str, str]:
    """Hash tracked and nonignored untracked bytes/modes, excluding runtime evidence.

    HEAD is checked separately. Ignored external/environment inputs are declared
    in evidence applicability and judged by the reviewer, as with ticket evidence.
    """
    root = Path(run.repo_root).resolve()
    data_dir = Path(run.cfg.defaults.data_dir)
    sessions = (data_dir if data_dir.is_absolute() else root / data_dir) / "sessions"
    paths = set(_git(run, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0"))
    files = {}
    for raw in sorted(paths - {b""}):
        from .spec_artifacts import managed_path
        if managed_path(os.fsdecode(raw)):
            continue
        path = root / os.fsdecode(raw)
        if path.is_relative_to(sessions.resolve()):
            continue
        if path.is_symlink():
            value = b"link:" + os.fsencode(os.readlink(path))
        elif path.is_file():
            value = str(path.stat().st_mode).encode() + b":" + hashlib.sha256(path.read_bytes()).digest()
        elif not path.exists():
            value = b"deleted"
        else:
            # Submodules need an explicit check; their directory bytes are not proof.
            value = b"directory:" + _git(run, "status", "--porcelain", "--", os.fsdecode(raw))
        files[os.fsdecode(raw)] = hashlib.sha256(value).hexdigest()
    return files


def tree_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


@dataclass
class ReceiptContext:
    prompt: str
    build_base: str
    work_item: SpecWorkItem | TicketWorkItem | None = None
    mandatory_checks: list[str] = field(default_factory=list)


def save(run, review: ReviewOutput, decision: ReviewDecision, context: ReceiptContext) -> Path:
    """Call inside a code phase; both the receipt and its location enter the trace."""
    item = context.work_item
    bound = run.session_dir / "work_item.json"
    if bound.exists():
        raw = json.loads(bound.read_text())
        saved = (SpecWorkItem if raw["kind"] == "spec" else TicketWorkItem).model_validate(raw)
        if item is not None and item != saved:
            raise ValueError("review target differs from the session binding")
        item = saved
    if item is None and (run.context_handoff_dir / "plan.md").is_file():
        # Preserve the same fallback target that the reviewer was instructed to use.
        plan_path = (run.context_handoff_dir / "plan.md").resolve().relative_to(run.repo_root.resolve()).as_posix()
        item = tickets.spec_work_item(run.repo_root, plan_path)
    if item is not None:
        tickets.validate_work_item(run, item, check_tree=False)
    reports = {}
    for value in review.artifacts:
        path = tickets.repo_path(run.repo_root, value)
        reports[value] = path.read_text()
    files = tree_files(run)
    receipt = ReviewReceipt(adw_id=run.adw_id, prompt=context.prompt, work_item=item,
                            baseline=baseline(run), tree_sha256=tree_digest(files), tree_files=files,
                            build_base=context.build_base, review=review, decision=decision,
                            mandatory_checks=context.mandatory_checks, reports=reports)
    dest = run.session_dir / "review-routing" / f"{run.phases[-1].seq:02d}.json"
    if dest.exists():
        raise ValueError(f"review receipt already exists: {dest}")
    tickets.atomic_json(dest, receipt.model_dump())
    return dest


@dataclass
class PreparedRecheck:
    source: ReviewReceipt
    request: RecheckRequest
    checks: list[str]
    changed: bool


def prepare_recheck(run, request: RecheckRequest, available: set[str], request_path: Path | None = None) -> PreparedRecheck:
    """Validate host source, exact definitions, current baseline and evidence hashes."""
    if (run.session_dir / "work_item.json").exists() or len(run.phases) != 1 or run.phases[0].seq != 1:
        raise ValueError("recheck requires a fresh session")
    tickets.verify_ref(run.repo_root, request.original_review)
    source_path = tickets.repo_path(run.repo_root, request.original_review.path, ".json")
    source = ReviewReceipt.model_validate_json(source_path.read_text())
    expected = run.session_dir.parent / source.adw_id / "review-routing"
    if source_path.parent.resolve() != expected.resolve() or source.adw_id == run.adw_id:
        raise ValueError("original_review must be a different host session's review receipt")
    if not gates.verdict_consistent(source.review, run).passed:
        raise ValueError("original review is inconsistent")
    if source.review.approved:
        raise ValueError("recheck requires an unapproved source review")
    if any(b.kind in {"spec_conflict", "ticket_conflict"} for b in source.review.blocking):
        raise ValueError("planning conflicts require planning revision and a new implementation binding")
    if source.work_item is not None:
        tickets.validate_work_item(run, source.work_item)
    if request.baseline != baseline(run):
        raise ValueError("recheck baseline must equal current full Git HEAD")
    blocker_ids = {b.id for b in source.review.blocking}
    for evidence in request.evidence:
        tickets.verify_ref(run.repo_root, evidence.artifact)
        if not set(evidence.resolves) <= blocker_ids or not evidence.applicability.strip():
            raise ValueError("new evidence must name original blockers and explain applicability")
    current = tree_files(run)
    supplied = {e.artifact.path for e in request.evidence}
    if request_path is not None:
        supplied.add(request_path.resolve().relative_to(run.repo_root.resolve()).as_posix())
    # Newly supplied evidence/manifest files do not change implementation identity.
    # Existing source/config files relabelled as evidence still invalidate it.
    for path in supplied - source.tree_files.keys():
        current.pop(path, None)
    changed = source.baseline != request.baseline or source.tree_files != current
    checks = sorted(set(source.mandatory_checks) | set(request.checks)
                    | {c for b in source.review.blocking for c in b.checks})
    if changed and not checks:
        raise ValueError("code/configuration baseline changed; specify affected configured checks for revalidation")
    missing = set(checks) - available
    if missing:
        raise ValueError(f"recheck workflow capability missing for checks {sorted(missing)}")
    # Bind only after all inputs are validated. No implementation agent is invoked.
    tickets.bind_work_item(run, AgentCall(output_type=ReviewOutput, prompt=source.prompt,
                                         work_item=source.work_item), "reviewer")
    return PreparedRecheck(source, request, checks, changed)


def recheck_notes(prepared: PreparedRecheck) -> str:
    return ("Recheck the original target and ALL current obligations. New evidence is a claim, "
            "not automatic closure. Reassess its applicability; never reuse invalidated proof.\n"
            + json.dumps({"original_review": prepared.source.model_dump(),
                          "new_evidence": prepared.request.model_dump(),
                          "baseline_changed": prepared.changed}, ensure_ascii=False))


def acceptance_decision(review, policy, results, mandatory) -> ReviewDecision:
    """An ADW cannot override failed/missing deterministic checks with approval."""
    decision = decide(review, policy)
    if decision.action != "approve":
        return decision
    missing = set(mandatory) - results.keys()
    failed = [name for name, result in results.items() if not result.passed or not result.applicable]
    if missing or failed:
        return ReviewDecision(action="handoff", owners=["quality"],
                              reason=f"mandatory checks missing {sorted(missing)} or current checks failed/stale {failed}; not accepted")
    return decision


def evidence_notes(results: dict) -> str:
    return "Current deterministic check evidence (inspect actual logs):\n" + json.dumps(
        {name: result.model_dump() for name, result in results.items()}, ensure_ascii=False)


def verification_fingerprint(run) -> str:
    return baseline(run) + ":" + tree_digest(tree_files(run))


def refresh_results(run, results: dict) -> dict:
    """Keep actual exit status intact while invalidating evidence from older inputs."""
    if not results:
        return results
    current = verification_fingerprint(run)
    return {name: result.model_copy(update={"applicable": result.input_fingerprint == current})
            for name, result in results.items()}
