# Install or update SSSF

Run from the target repository root:

```bash
uv run .agents/skills/sssf/scripts/install.py
```

Prerequisites are Python 3.10+, `uv`, Git, SQLite, and authenticated Codex CLI
access. `just` is optional; Bun is needed only for the visualizer.

The installer classifies files before writing:

- config, prompts, workflows, `quality.py`, `.env.sample`, and `justfile` are
  user-owned and are preserved when already present;
- runtime modules and the read-only child role are managed;
- managed updates apply automatically only when the current digest matches the
  previous `.sssf/manifest.json` entry;
- managed local edits stop the entire install before any write.

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
uv run adws/adw_prompt.py "summarize this repo" --agent scout
```

A green smoke means config preflight, thread creation, structured output,
permissions, gates, and SQLite tracing all completed.

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
Compare every installed `adw_plan*.py` and any other planner chain (including
`adw_simple_sdlc.py`) with its current template. Merge the required mutually
exclusive CLI target arguments, `PlanningTarget` construction, and
`AgentCall.planning_target` together. For `adw_plan_decompose`, forward the
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
