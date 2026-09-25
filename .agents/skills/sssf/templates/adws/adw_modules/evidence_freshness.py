"""Scout-owned applicability of prior evidence, before any delivery/recheck work."""
from __future__ import annotations

import json

from . import tickets
from .data_types import AgentCall, EvidenceScoutOutput, GateReport, PhaseParams, TicketWorkItem


def dependency_refs(item):
    return list(item.dependency_evidence) if isinstance(item, TicketWorkItem) else []


def coverage(refs):
    expected = {(ref.path, ref.sha256) for ref in refs}

    def gate(output, run):
        actual = [(a.path, a.sha256) for a in output.assessments]
        return (GateReport()
                .check("evidence coverage", set(actual) == expected and len(actual) == len(expected),
                       "assess every supplied artifact exactly once, retaining its hash")
                .check("assessment reasons", all(a.reason.strip() for a in output.assessments),
                       "each verdict needs a concrete applicability reason")
                .check("temporary result", not output.artifacts,
                       "freshness investigation returns inline results, not report artifacts"))
    return gate


def inspect(run, refs, work_item=None):
    """Return a temporary scout decision; skip when no prior evidence is consumed."""
    refs = list({(r.path, r.sha256): r for r in refs}.values())
    if not refs:
        return None
    for ref in refs:
        tickets.verify_ref(run.repo_root, ref)
    prompt = ("Evidence freshness investigation. Briefly inspect the supplied records, their "
              "referenced proof and relevant intervening changes. Judge applicability to the "
              "current code, tests, shared dependencies, configuration and environment. "
              "Different HEADs and unrelated edits alone do not make evidence stale. "
              "A historical failed/pending obligation is context, not a claim that it passed. "
              "Do not implement, run tests, delegate, or perform a full review. "
              "Return the result inline only, with artifacts=[]; do not write a report file. "
              "Return EvidenceScoutOutput with exactly one assessment per supplied artifact: "
              "applicable, stale, or uncertain, with concrete reasons and file references. "
              "Use status=success for a completed investigation even when stale/uncertain; "
              "the host will fail immediately on either verdict. Evidence to inspect:\n"
              + json.dumps([r.model_dump() for r in refs], ensure_ascii=False))
    with run.phase(PhaseParams(name="evidence_scout", kind="agent", owner="scout",
                               description="Briefly investigate whether prior evidence still applies before using it")) as ph:
        result = ph.call(AgentCall(output_type=EvidenceScoutOutput, prompt=prompt,
                                   work_item=work_item, gates=[coverage(refs)]))
        for ref in refs:
            tickets.verify_ref(run.repo_root, ref)
    return result


def failure_reason(result):
    if result is None:
        return ""
    rejected = [a for a in result.assessments if a.verdict != "applicable"]
    if not rejected:
        return ""
    return "scout 证据调查未通过：" + "; ".join(
        f"{a.path} [{a.verdict}]: {a.reason}" for a in rejected)


def notes(result):
    if result is None:
        return ""
    return ("Prior evidence freshness was decided by scout. Use this temporary decision; "
            "do not repeat or override the freshness investigation. Assess the assigned "
            "implementation/acceptance obligations and current check results normally.\n"
            + result.model_dump_json())
