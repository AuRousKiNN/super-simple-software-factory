# Reviewer routing and explicit recheck

`ReviewOutput.status=success` means review execution completed. An unapproved
review remains a successful agent phase and reaches the Python workflow.
`status=fail` keeps the existing runtime failure path; it is not a business route.

## Output and routing

Each `blocking` entry is a `ReviewBlocker`: id, kind, owner, description, basis,
trigger, consequence, evidence, closure and handoff. `basis` covers exact unmet
finding requirement strings and pending `required_verification` IDs. The latter
list records all mandatory obligations assigned to this review; satisfied entries
need evidence, and pending entries prevent approval. Continuity gates preserve
requirements and mandatory obligations across repair and recheck rounds.

| kind | owner | Workflow action |
|---|---|---|
| implementation / test_implementation | builder | Bounded in-scope repair |
| check_execution | quality | Execute configured check IDs in a code phase |
| spec_conflict | planner | End, then revise root contract in a new planning session |
| ticket_conflict | decomposer | End, then revise ticket allocation in a new decomposition session |
| environment | environment | End with missing conditions, unrun checks and impact |
| manual_validation | human | End with preconditions, steps and pass_criteria |
| external_regression / protocol_issue | external | End with evidence and responsible scope |

Only check_execution uses `checks` (stable IDs, never shell commands). Integrated
issues identify affected_tickets and invalidated_evidence. Optional Smoke and
ordinary advice stay in report residual risks. Gate checks cover structure and
internal consistency; reviewers judge credibility, sufficiency and ownership.

All mixed issues are saved. Root-contract conflicts take priority; any external
or planning blocker prevents automatic edits. Automatic mixed work is admitted
only when repair budget, verification budget and configured checks can close it.
Budget exhaustion or missing capability saves a handoff rather than guessing a
command or invoking a builder without a repairable problem.

`adw_build_review.py` and `adw_simple_sdlc.py` allow three builder repairs and two
supplementary verification rounds. Initial building is not a repair. SDLC runs
its mandatory test suite before each review of changed builder output; previously
executed supplementary checks are also renewed after edits. Each result carries
the execution input fingerprint; later code/configuration changes invalidate its
applicability even when the command exited successfully. Reviewer gate
corrections have their own one-turn budget; JSON repair retains its separate
runtime budget. Generated one-shot workflows provide no repair/verification loop:
an unapproved review saves its reason and stops before downstream delivery.

## Check configuration and acceptance

Edit `quality.check_specs()` to bind stable IDs to explicit argv lists. Shipped
IDs are test, lint, typecheck and build. Placeholder echo commands are not
configured capabilities and cannot pass verification. Reviewer-provided strings
are looked up in this registry, never executed as shell commands. Inspect actual
commands/logs, scope and assertions to assess evidence sufficiency.

Approval plus applicable passing mandatory checks permits downstream delivery.
SDLC commits implementation and documentation only after that decision. These
starter workflows do not publish ticket-acceptance records. Custom acceptance
ADWs must establish all checks, manual validation, applicability and integrated
obligations, finish accepted, and only then call `tickets.record_acceptance`.

## Durable handoff and planning changes

Each route code phase saves a host-owned immutable
`<data_dir>/sessions/<adw_id>/review-routing/<phase-seq>.json`. It includes the
complete ReviewOutput, copied report text, decision, original prompt, effective
work_item, HEAD, implementation file hashes, diff base and mandatory check IDs.
The trace logs its location and exact reason. Business blockers call
`run.finish(accepted=False, reason=...)`; the database/UI show this execution as
not accepted and runtime resources are released. Agents cannot edit these
receipts, including in ignored session directories.

For planning conflicts, pass the original receipt and affected scope/needed
decision explicitly to planner or decomposer in a **new session**. Revise the
stable planning paths in place, increment changed documents' revision, publish
and refresh affected indexes, then bind current definitions in a **new
implementation session**. Do not reuse an old content-hash binding or bypass
prerequisite evidence validation. Recheck rejects planning-conflict sources.

## Evidence-only recheck

After environment or required manual conditions are resolved, create a JSON file
matching `RecheckRequest`. Paths below are repository-relative; sha256 values are
full hashes of the referenced files, and baseline is the full current Git HEAD.
For example (replace bracketed values):

```json
{
  "original_review": {
    "path": "adws/adw_data/sessions/old-id/review-routing/07.json",
    "sha256": "<sha256 of original receipt>"
  },
  "baseline": "<full current HEAD>",
  "evidence": [{
    "artifact": {"path": "evidence/manual.md", "sha256": "<sha256 of evidence>"},
    "resolves": ["B-1"],
    "applicability": "Executed the required steps on this code/configuration and the restored staging environment."
  }],
  "checks": ["test"]
}
```

Invoke:

```bash
uv run adws/adw_recheck.py recheck.json --config adws/adw_sssf_config/sssf.config.yaml
```

This entry always creates a new session, validates the source receipt/target
identity/definition hashes, current HEAD, evidence hashes and blocker references,
then executes required configured checks and reviews. It never starts a builder.
New evidence and request files alone do not invalidate code; rewriting existing
implementation/configuration files does. If HEAD or tracked/nonignored code
changes, affected configured checks must be supplied. The reviewer must reassess
all inherited obligations and evidence applicability even when code is unchanged.
Ignored external/environment inputs are covered by explicit applicability claims
and independent review, not by the Git file fingerprint.

A ticket recheck retains the existing conservative dependency-evidence rules:
stale dependency HEAD or dirty prerequisite baseline needs an explicit custom
revalidation/acceptance workflow first. Core does not waive these constraints.
The recheck result is a new receipt and a run verdict, not automatic continuation
of the earlier program counter or issuance of a ticket-acceptance record.

## Existing installations

Install/update retains customized prompts, ADWs and quality.py. Explicitly merge
ReviewOutput, reviewer system/user/report examples, verdict gates, review call
sites, routing branches and quality registry together. Existing string blockers
are not accepted by the new contract. No runtime/SQLite schema bump, importer or
silent compatibility fallback is added. Gate correction is for current output
consistency, not migration of customized prompts.
