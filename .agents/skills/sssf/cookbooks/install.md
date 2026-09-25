# Install or update SSSF

Run from the target repository root:

```bash
uv run .agents/skills/sssf/scripts/install.py
```

Prerequisites are Python 3.10+, `uv`, Git, SQLite, and authenticated Codex CLI
access. `just` is optional; Bun is needed only for the visualizer.

The installer classifies files before writing:

- config, prompts, workflows, `quality.py`, and `justfile` are
  user-owned and are preserved when already present;
- runtime modules and the read-only child role are managed;
- managed updates apply automatically only when the current digest matches the
  previous `.sssf/manifest.json` entry;
- managed local edits stop the entire install before any write.

The installer adds the SSSF machinery (`.agents/skills/sssf/`, `adws/`,
`.sssf/`, the SSSF Codex role, and the installed `justfile`) to `.gitignore`.
Specification artifacts under `specs/` remain trackable and are the only SSSF
artifacts intended for the target repository's history. The installer does not
create an environment-variable example file.

Every changing install creates `.sssf/backups/<snapshot>/`. The snapshot covers
all touched targets plus the active config and SQLite database files. A write
failure restores the pre-install state automatically.

After reviewing a managed conflict:

```bash
uv run .agents/skills/sssf/scripts/install.py --force-managed
```

This option does not overwrite user-owned files. Merge updated config or prompt
examples manually.

To return to the immediately preceding verified distribution, first stop all
workflows and then run:

```bash
uv run .agents/skills/sssf/scripts/install.py --rollback latest
```

Rollback refuses when any captured file changed after installation. That guard
prevents erasing new workflow runs or later engineering edits.

Verify a fresh install:

```bash
codex --version
codex login status
just demo
just sessions
```

Without `just`, run:

```bash
uv run adws/adw-prompt.py "summarize this repo" --agent scout
```

A green smoke means config preflight, thread creation, structured output,
permissions, gates, and SQLite tracing all completed.

## Upgrading to Approve for me

Update managed runtime modules and the managed `sssf_recon` role with the
installer. It preserves the user-owned roster. In the installed YAML, replace
`codex.approval_policy: never` with `codex.approval_mode: auto_review`, retaining
all models, role prompts, write contracts and project-specific checks. The old
key is rejected rather than silently continuing with denied approvals.

All roles now use SDK `ApprovalMode.auto_review` on start, resume and each turn.
Recon children explicitly select on-request approvals and the automatic reviewer
with their read-only sandbox. Command network access inside the sandbox remains
disabled; the runtime reviews requested exceptions. Do not add a local approval
handler that unconditionally accepts requests.

Start a new session after this policy change. Existing mappings include the
runtime configuration fingerprint and must not be rewritten to force a resume.
This upgrade requires no trace schema change and never rewrites old run records.

## Upgrading to ticket decomposition

The installer preserves existing roster, prompts and starter ADWs. It ships new
files and prints this merge checklist; there is no silent legacy-contract fallback.
Before invoking a new workflow:

1. Merge the decomposer entry from templates/sssf.config.yaml, including its
   planning writes and enabled read-only recon role. Keep builder children disabled.
2. Merge planner prompts: full root spec with stable REQ/CONTRACT/AC IDs and the
   authoritative `spec_path` in PlanOutput. Move ticket ownership to decomposer.
   Merge the in-place revision rules for both roles: stable file paths and an
   integer revision in each document, incremented for every revision.
3. Merge builder/reviewer prompts: render `{{work_item}}`, preserve target on every
   repair, and treat root/set/index as read-only. Merge new decomposer prompts.
4. Merge customized ADWs: use concrete output types, construct SpecWorkItem after
   planning, preserve it for fixes/review, and select an explicit ticket after
   decomposition. Use the stable `<spec>.tickets/` directory and refresh indexes
   through `tickets.publish_decomposition`. Add required roster entries and
   `run.finish()` acceptance.
5. Validate the merged config and contracts using an isolated spec → decomposition
   → single-ticket smoke. Start fresh sessions for the updated decomposition input
   contract and changed prompt fingerprints. Existing session records remain audit
   records; new runs use the current source and planning paths.

Detailed schemas and prerequisite evidence format: [ticket contract](../references/tickets.md).

Reviewer routing updates require an explicit merge of the customized reviewer
prompts/report examples, review ADWs and `quality.py` check registry with the
managed ReviewOutput/gates/routing modules. String blockers are replaced by
structured ReviewBlocker entries. Preserve local commands when moving them into
`quality.check_specs()`. See [routing migration](../references/reviewer-routing.md#existing-installations).

## Upgrading specification artifacts

Merge preserved ADWs, planner/documenter prompts and roster together. Planning
requires `--spec-dir` or `--spec`; new specs use `specs/<spec_key>/spec.md`.
Compare every installed `adw-plan*.py` and any other planner chain (including
`adw-simple-sdlc.py`) with its current template. Merge the required mutually
exclusive CLI target arguments, `PlanningTarget` construction, and
`AgentCall.planning_target` together. For `adw-plan-decompose`, forward the
planner's `plan.spec_path` to `tickets.decompose`; the ticket directory becomes
`specs/<spec_key>/spec.tickets/`. Verify each merged script with `--help` before
launch: its usage must show `(--spec-dir SPEC_DIR | --spec SPEC)`. Updating this
skill or running the installer alone does not migrate preserved script CLIs.

Documenter writes are `[]` (only current reports and handoff exceptions); its
output is `DocumentDraftOutput`. Business ADWs collect `DocumentRequest`, publish
through `spec_artifacts.document`, and optionally prepare post-finish projection.
The installer retires `adw_document.py` with a rollback snapshot; remove custom
calls to that standalone workflow. Existing user-owned ADWs are not overwritten.
Do not use a flat old spec as a new-layout publication target: explicitly migrate
its spec/ticket paths and verify references before starting a new session.
See [spec artifact contract](../references/spec-artifacts.md).

## Workflow consolidation and hyphenated entry points

The distribution ships nine `adw-*.py` entries. `adw-build` is the only build-first
entry and includes checks, review, bounded repairs, documentation and commits.
`adw-simple-sdlc` plans before entering that same shared delivery chain.
Build-test, build-review and all three plan-build variants are retired.

The installer removes recorded, untouched legacy workflow files with transactional
backup. Customized or unrecorded legacy files are reported as conflicts even with
`--force-managed`: review and merge them into the new entry, then move the old file
out of the active ADW directory before installing again. New hyphenated workflows
remain user-owned. Update custom launch scripts and preserved justfile recipes.

Merge `quality.required_checks()` and `quality.not_applicable_checks()` alongside
the project's real command registry: quality.py is preserved on upgrade. Update
reviewer prompts so ticket required-verification evidence names actual files.
No generic distribution can infer the target repository's correct test command.

## Upgrading automatic ticket delivery

Merge the preserved `adw-build.py` entry to call `delivery.launch(run, target)`.
`--ticket-set` becomes optional; normal callers supply only `--ticket`. Generated
builder chains use the shared delivery module and retain its frozen-input guard.
Keep configured quality commands unchanged. No observability schema change is needed.

Stop workflows before upgrading. Historical acceptance files remain immutable.
For existing schema-v2 records, explicitly add chronological sidecars from successful
host session completion timestamps and stable database row order:

```bash
uv run --with pydantic --with pyyaml --with python-dotenv --with rich \
  .agents/skills/sssf/scripts/migrate_ticket_history.py
```

The migration is idempotent, acquires the workspace lock, validates all source
records and referenced proof before writing, and never changes original receipt
bytes. Missing successful trace timestamps fail explicitly; do not use mtimes or
invent acceptance times. Old records can still be explicitly supplied with
`--dependency-evidence`, subject to normal validation and scout applicability.
