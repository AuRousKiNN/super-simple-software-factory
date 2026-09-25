# Ticket contract

Root specs remain independently implementable. Planner returns the authoritative
repository-relative `spec_path`; the handoff plan is a copy. REQ, CONTRACT and AC
IDs provide stable references. Decomposer owns ticket boundaries and dependencies.

## Artifacts and validation

A source `specs/example/spec.md` has its current planning files at `specs/example/spec.tickets/`:
`ticket-set.md`, `tickets/TICKET-*.md`, and a host-derived `index.json`.
The set frontmatter contains schema_version=1, revision, source_spec and tickets.
Each ticket contains schema_version=1, id, revision, kind, profile, blocked_by and
requirements. kind is behavior/refactor/integration; profile is standard/critical.

Planner and decomposer revise their documents in place at stable paths. Each spec,
set and ticket starts with revision=1 in its frontmatter; every revision increments
that document's number, including wording and ordering changes. Unchanged documents
retain their number. Planning roles own this count; the index mirrors document
metadata and the runtime binds exact content hashes.

Markdown layout and metadata examples guide the agent; they are not artifact
validation rules. The runtime does not check headings, body content, AC tables,
identifier patterns, revision/profile values, extra keys or frontmatter schemas.
There is no decomposer-specific artifact gate or format-repair turn.

`read_set` reads source_spec and tickets metadata for indexing, treats ticket bodies
as opaque, and supplies defaults for optional ticket metadata. YAML is read with
safe_load; parsing is input consumption, not a schema gate. Missing source/member
files or unreadable required metadata prevent the code phase from consuming them.
Paths, source hashes, membership identities and acyclic blocker relationships are
runtime constraints. Coverage and evidence quality remain agent/reviewer judgment.

`prepare_decomposition` saves the source digest and stable output directory before
dispatch. A fresh session can revise the existing files even when an index already
exists. Valid fail envelopes persist and stop immediately with needs_spec_revision,
needs_decision or artifact_error. The code index phase uses `publish_decomposition`
to check the bound source and output directory, refresh the index, and save its
path and digest in the host-owned session file `decomposition-published.json`.
That receipt closes the session's planning binding; further revisions use a fresh
session with the same output directory. `load_index` rejects changed inputs until
the index is regenerated.

## Implementation inputs

`AgentCall.work_item` is a discriminated SpecWorkItem/TicketWorkItem union.
ArtifactRef has path and sha256. Use `spec_work_item` or `ticket_work_item` to
assemble input. `tickets.select_ticket` requires an explicit TicketSelection;
`make_adw --agents planner,decomposer,builder` generates `--ticket-id` support.
Generated standalone decomposer workflows take a spec path as their argument.

`work_item.json` binds one target and exact definitions per session. Every repair
and review retains that target while `previous` carries fresh feedback. Runtime
also restores a saved target when a repair call omits work_item. A changed target
or source cannot reuse that session. The root spec, complete ticket set and index
are protected ahead of broad writes. Decomposer may write only its current output
planning files. Ignored bound inputs and host binding/acceptance files participate
in snapshots and restoration on all terminal paths.

## Evidence and acceptance

`--dependency-evidence` names a JSON array of ArtifactRef objects pointing to
`<data_dir>/sessions/<adw_id>/ticket-acceptance.json`. Each AcceptanceRecord records
schema_version=1, accepted=true, adw_id, ticket_id, definition_sha256, baseline,
checks, reviews, manual_validation and applicability. Checks/reviews are nonempty
artifact-reference lists. Each blocker needs exactly one record. Current source
hashes and evidence hashes must match. The recorded Git HEAD identifies where
acceptance was observed; it need not equal the current HEAD. Initial binding still
requires a clean implementation baseline. Before builder, one read-only scout
briefly investigates the records and relevant changes to code, tests, dependencies,
configuration and environment. Unrelated commits do not require reissuing evidence.
The scout returns a temporary inline decision with no report artifacts. No
`evidence-freshness.json` or scout Markdown report is written for this investigation;
normal runtime tracing remains. Any `stale` or `uncertain` assessment immediately finishes with accepted=false and exit code 1, including the evidence
path and scout's reason. No builder, checks, reviewer, documenter or automatic
revalidation runs after rejection. Builder/reviewer consume scout's decision
without repeating or overriding freshness judgment. Required checks and independent
acceptance review still run after an applicable verdict; freshness is not acceptance.

`record_acceptance(run, record)` is a host-only API after `run.finish(accepted=True)`.
`adw-build` and ticket-mode `adw-recheck` establish actual check execution, review
acceptance, required validation and applicability through the shared delivery module. Core validates identity,
current baseline and referenced artifacts; it does not infer semantics from logs.
Generated builder chains use this same delivery module. A ticket is an input
mode, never a separate workflow.
Whole-spec integration remains a separate ADW acceptance obligation; ready ticket
definitions or a successful builder call do not prove integrated completion.

New entry points validate required roster and finish after the code index phase.
No changes to runtime or SQLite schema version 2 are required; planning artifact
schema version 1 is independent.

## Current-baseline revalidation

Use `adw-recheck` with an original ticket review receipt (approved or blocked),
current `baseline`, optional new `evidence`, and optional `dependency_evidence`
containing refreshed ArtifactRefs. Revalidate affected prerequisites in topological
order in separate explicitly requested work when their evidence no longer applies;
never rewrite an old acceptance baseline. Recheck first runs the evidence scout and
stops on stale/uncertain evidence. Only after the scout admits the evidence do ticket
rechecks run all required quality checks and independent review without builder. Required
verification entries must reference real repository-relative evidence files.
Store request manifests and transient evidence under the ignored session data
area, or commit them before revalidation; ticket publication needs a clean baseline.

Delivery documents and commits before finishing. Host finish projection may commit
only managed documentation; unchanged implementation content is checked again.
Acceptance records the observed HEAD and immutable check/review artifacts; later
reuse depends on applicability.
For a completed documented scope, publishing its ticket receipt does not modify
repository files or create another commit. A failed publication exits nonzero;
use a fresh `adw-recheck` session to establish and publish current evidence.

先前证据不要求与当前 HEAD 完全一致。多个前置票据可以使用不同提交上的验收记录，
使用先前证据前，由一个只读 scout 简单调查相关代码、测试、配置和环境是否仍适用；
无关改动本身不使证据过时。scout 判定已过时或无法确认时，ADW 直接返回临时调查结果并立即失败，
不进入 builder、质量检查或 reviewer，也不自动补验。builder/reviewer 不重复判断证据新鲜度。
证据哈希、目标定义和必需检查仍受校验。
票据重验文档保存在会话目录中，不创建提交；正常 build 仍发布并提交规格执行文档。
