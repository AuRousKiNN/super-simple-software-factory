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
