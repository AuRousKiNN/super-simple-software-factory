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
3. Merge builder/reviewer prompts: render `{{work_item}}`, preserve target on every
   repair, and treat root/set/index as read-only. Merge new decomposer prompts.
4. Merge customized ADWs: use concrete output types, construct SpecWorkItem after
   planning, preserve it for fixes/review, and select an explicit ticket after
   decomposition. Add required roster entries and `run.finish()` acceptance.
5. Validate the merged config and contracts using an isolated spec → decomposition
   → single-ticket smoke. A changed prompt fingerprint requires a new session.

Detailed schemas and prerequisite evidence format: [ticket contract](../references/tickets.md).
