# Review Task

## Variables

### prompt

{{prompt}}

### work_item

{{work_item}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

1. Establish the target from `work_item`; if absent, read
   `<context_handoff_dir>/plan.md` when it exists, else use `prompt`. Identify the
   current review scope, applicable contracts, and any assigned integration
   obligations without expanding a ticket to cover the whole feature.
2. Read `previous_envelope.diff_path` and the relevant files in its deterministic
   `changed_files` index. Independently inspect the implementation, callers,
   assertions, and available evidence, including sources referenced by the build
   handoff. Confirm their applicability to the current code and pending changes.
3. Rule on every requirement in scope, with one `findings` entry per requirement.
   Shared evidence may support several entries. Apply the blocking criteria from
   the system instructions and identify the minimum sufficient closure for each
   actual gap; distinguish missing proof from missing extra tests.
4. Classify every blocker using the exact system kind/owner pairs. List all assigned
   mandatory obligations in required_verification, preserving source obligations
   during recheck and evaluating the newly supplied evidence.
5. Write `<context_handoff_dir>/review.md` using the guidance below, then emit your
   Report JSON. Keep test execution and final acceptance with their owning phases.

## Save and hand off

Scale the report to the target, merging or omitting inapplicable sections. Include:

- **Target and baseline:** bound spec/ticket or direct request, relevant code and
  pending changes, applicable contracts/profile, prerequisites, and assigned
  integration scope. Use existing identities rather than inventing round numbers.
- **Requirements and evidence:** each requirement's judgment with code, assertion,
  and evidence references. Distinguish inspected, executed by other phases, reused,
  and pending evidence. For execution, identify command, working directory/filter,
  exit status, actual execution scope, key assertions, and output/log source. For
  reused evidence, state its source and current applicability. Explain why the
  selected evidence is sufficient or what remains unconfirmed; avoid duplicating
  shared evidence for every AC/CASE.
- **High-impact boundaries:** relevant triggers, outcomes, and prohibited side
  effects; the real components exercised and external boundaries substituted.
  Explain when local evidence cannot establish the claimed complete path.
- **Blocking items:** one independent gap per item, its requirement/contract basis,
  realistic trigger, consequence, supporting evidence or insufficiency of existing
  proof, structured kind, owner, stable id, handoff and minimum sufficient closure. Name any unmet explicit verification
  obligation. Keep ordinary suggestions separate.
- **Integration judgment, when assigned:** the combined outcome and global
  obligations on the final relevant baseline, evidence used, and any affected
  tickets. Distinguish valid individual conclusions from missing overall evidence;
  identify individual evidence invalidated by an actual integration defect.
- **Residual risks and remaining obligations:** non-blocking observations,
  unconfirmed boundaries and their impact, pending checks owned by later phases,
  and the necessary repair or verification handoff. State what this approval covers.

Use `<context_handoff_dir>/review.md` for this report; leave other roles' artifacts
and host acceptance records intact. Use repository-relative POSIX paths in the
structured `artifacts` field and declare only files actually saved. Keep evidence,
risks, and integration detail in the report and existing fields, without adding
JSON fields or another acceptance protocol.

## Report

Respond with ONLY valid JSON matching `ReviewOutput` — no prose before or after:

```json
{
  "status": "success",
  "approved": false,
  "summary": "<review target and concrete unmet outcome or evidence obligation>",
  "findings": [
    { "requirement": "<existing identifier when available and the requested behavior>", "met": false, "evidence": "<code/assertion/evidence reference and the exact gap>" }
  ],
  "blocking": [{
    "id": "B-1",
    "kind": "check_execution",
    "owner": "quality",
    "description": "<existing required check has not run>",
    "basis": ["<existing identifier when available and the requested behavior>", "V-1"],
    "trigger": "<real path whose behavior remains unconfirmed>",
    "consequence": "<consequence if that behavior fails>",
    "evidence": ["<available proof and why it is insufficient>"],
    "closure": "<minimum sufficient execution evidence>",
    "handoff": "<pending check, scope, owner and applicability conditions>",
    "checks": ["test"],
    "preconditions": "",
    "steps": [],
    "pass_criteria": "",
    "affected_tickets": [],
    "invalidated_evidence": []
  }],
  "required_verification": [{
    "id": "V-1", "description": "<explicit verification obligation>",
    "satisfied": false, "evidence": []
  }],
  "artifacts": ["<repository-relative context_handoff_dir>/review.md"],
  "notes_for_next_agent": "<necessary repair or verification, approval scope, and remaining obligations>"
}
```

`status` is `success` when the review itself completed, including a supported
rejection; it is not the verdict. `approved` is true only when every requirement
in scope is credibly met, required verification for that verdict is satisfied,
`findings` has no unmet entry, and `blocking` is empty. For approval, report the met
requirements and their evidence with `blocking: []`. Suggestions and residual
risks alone do not justify rejection. Final acceptance remains the ADW's decision.
