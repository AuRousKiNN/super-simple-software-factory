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
hashes and evidence hashes must match. Core uses exact Git HEAD equality and
requires a clean implementation baseline on initial binding; older evidence must
be revalidated by the ADW for the current code, tests, configuration and environment.
A bound session may repair its own implementation without treating those edits as
new prerequisite evidence. A fresh session must recheck them.

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
containing refreshed ArtifactRefs. Revalidate prerequisites in topological order;
never rewrite an old acceptance baseline. Ticket rechecks always run all applicable
required quality checks and independent review without invoking builder. Required
verification entries must reference real repository-relative evidence files.
Store request manifests and transient evidence under the ignored session data
area, or commit them before revalidation; ticket publication needs a clean baseline.

Delivery documents and commits before finishing. Host finish projection may commit
only managed documentation; unchanged implementation content is checked again.
Acceptance binds the resulting exact HEAD and immutable check/review artifacts.
For a completed documented scope, publishing its ticket receipt does not modify
repository files or create another commit. A failed publication exits nonzero;
use a fresh `adw-recheck` session to establish and publish current evidence.

票据重验的文档保存在会话目录中，不修改规格目录或创建提交。这样多个前置票据可以在
同一个 HEAD 上重新验收，并共同作为下游依赖证据。正常 build 仍发布并提交规格执行文档。
