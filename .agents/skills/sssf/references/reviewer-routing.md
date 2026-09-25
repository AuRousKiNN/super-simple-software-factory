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

`adw-build.py` and `adw-simple-sdlc.py` allow three builder repairs and two
supplementary verification rounds. Initial building is not a repair. Both workflows run
all applicable mandatory checks before reviewing changed builder output; previously
executed supplementary checks are also renewed after edits. Each result carries
the execution input fingerprint; later code/configuration changes invalidate its
applicability even when the command exited successfully. Reviewer gate
corrections have their own one-turn budget; JSON repair retains its separate
runtime budget. Generated builder workflows enter the same bounded delivery chain. Other generated
review-only chains save an unapproved verdict and stop.

## Check configuration and acceptance

Edit `quality.check_specs()` to bind stable IDs to explicit argv lists. Shipped
IDs are test, lint, typecheck and build. Placeholder echo commands are not
configured capabilities and cannot pass verification. Reviewer-provided strings
are looked up in this registry, never executed as shell commands. Inspect actual
commands/logs, scope and assertions to assess evidence sufficiency.

Approval plus applicable passing mandatory checks permits downstream delivery.
SDLC commits implementation only after approval. It also publishes execution
records for controlled handoffs, preserving the unaccepted result and leaving
unfinished code uncommitted. Ticket-mode build and recheck establish all checks, required validation and
applicability, finish accepted, then call `tickets.record_acceptance`. Required
verification evidence must use real repository-relative file paths in ticket mode.

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
uv run adws/adw-recheck.py recheck.json --config adws/adw_sssf_config/sssf.config.yaml
```

This entry always creates a new session, validates the source receipt/target
identity/definition hashes, current HEAD, evidence hashes and blocker references,
then runs one brief read-only scout investigation before checks or review. The
scout assesses the original review, dependency records and supplied evidence against
relevant code, tests, shared dependencies, configuration and environment. HEAD or
whole-tree changes alone do not establish staleness. Historical failed/pending
obligations remain context, not claims of success. The host consumes the temporary
inline decision without writing `evidence-freshness.json` or a scout Markdown report;
normal runtime tracing remains. A stale or uncertain verdict immediately
finishes with accepted=false and exit code 1, returning paths and reasons; no checks,
builder, reviewer, documenter or automatic refresh follows. When all evidence is
applicable, configured checks and review proceed. The reviewer uses scout's decision
without repeating freshness judgment and still reviews all acceptance obligations.

A ticket recheck accepts both blocked and approved source reviews. Prerequisite
records may come from different HEADs when their evidence still applies. Supply
refreshed `dependency_evidence` when affected prerequisites need revalidation, in
topological order in separately requested work. Scout alone decides applicability;
unrelated commits are not grounds to reject or reissue prior evidence.
An approved source may use `evidence: []`; every applicable check is rerun and the
reviewer reassesses all obligations. Successful ticket recheck publishes canonical specification documentation, commits only
those documents, finishes and publishes a new current-baseline ticket acceptance. It never resumes
an old program counter or promotes a ticket to whole-spec integration acceptance.

## Existing installations

Install/update retains customized prompts, ADWs and quality.py. Build/recheck now
require the existing `scout` roster entry; merge the scout system/user templates and
entrypoint changes too. Evidence investigation uses `EvidenceScoutOutput`, a separate
`evidence_scout` runtime identity, and enforces no children and read-only writes
without altering the general scout configuration. Explicitly merge
ReviewOutput, reviewer system/user/report examples, verdict gates, review call
sites, routing branches and quality registry together. Existing string blockers
are not accepted by the new contract. No runtime/SQLite schema bump, importer or
silent compatibility fallback is added. Gate correction is for current output
consistency, not migration of customized prompts.

When upgrading an existing installation, merge `adw-recheck.py` together with
`adw_modules/delivery.py`: the entrypoint must pass the review receipt and
`accepts_scope=True` to finish projection, and ticket rechecks must publish canonical
specification documents. Updating only the documenter prompt does not repair stale
acceptance metadata. Historical sessions and reports remain unchanged; a new recheck
records the current result.

Recheck includes a documenter stage: it passes the bound root/ticket, actual check
results, review receipt and supplied evidence through `DocumentRequest`. Empty
implementation diffs are valid and never fall back to the preceding commit.
Finish projects the actual verdict for the bound scope into the specification README
and index. Ticket recheck acceptance does not establish whole-spec integration acceptance. See [spec artifacts](spec-artifacts.md).

先前证据不要求与当前 HEAD 完全一致。多个前置票据可以使用不同提交上的验收记录，
使用先前证据前，由一个只读 scout 简单调查相关代码、测试、配置和环境是否仍适用；
无关改动本身不使证据过时。scout 判定已过时或无法确认时，ADW 直接返回临时调查结果并立即失败，
不进入 builder、质量检查或 reviewer，也不自动补验。builder/reviewer 不重复判断证据新鲜度。
证据哈希、目标定义和必需检查仍受校验。
重验会更新规格 README、执行记录和索引，并回写本次范围验收结果；成功的票据重验仅提交这些文档，按最终 HEAD 签发票据验收。历史失败记录保留，票据通过不代表整个规格通过。
