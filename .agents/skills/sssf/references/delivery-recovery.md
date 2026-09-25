# Recover a stopped delivery

Shared delivery records a host-owned `delivery-checkpoint.json` before builder
starts and after implementation/review transitions. Ordinary failures, Ctrl-C
and SIGTERM retain the final workspace after permission verification. Recovery
is explicit; a failed run never starts another attempt automatically.

```bash
# Keep a completed builder result; rerun failed/incomplete implementation if needed.
uv run adws/adw-build.py --resume <source-adw-id>

# Ask builder to retry using the retained work, with a fresh repair/verification budget.
uv run adws/adw-build.py --retry <source-adw-id>
```

Use the original `--config` when the source used a custom roster. Do not also
pass prompt, spec, ticket or dependency overrides: recovery inherits the frozen
target, request and selected prerequisite receipts. A new run ID is generated;
`--adw-id` may only name an unused destination. Source sessions and their failed
phase records, reports, commits and acceptance receipts are never rewritten.
Recovery uses new Codex threads and supplies retained structured context.

## What resumes

The entry covers `adw-build` (direct request, spec and ticket), and
`adw-simple-sdlc` or generated workflows **after they enter `delivery.execute`**.
Resume those deliveries through `adw-build --resume`, without rerunning planning.

- If builder failed/interrupted, its partial changes remain and builder runs
  again to complete the same target. A failed envelope never becomes success.
- If builder completed, `--resume` skips it. An interrupted repair invalidates
  that completed result, so its partial repair must go through builder again.
- Every attempt reruns all mandatory and previously selected checks, independent
  review, documentation and acceptance. Saved test/review approval is not reused.
  Prior review obligations are supplied again and cannot silently disappear.
- Resume preserves consumed repair/verification budgets. Retry explicitly resets
  those budgets and runs builder again without resetting, cleaning or stashing Git.
- Failure after a delivery commit can also be revalidated. The original diff base
  stays fixed, and current HEAD must equal the recorded terminal HEAD. A new
  successful attempt writes its own execution document and acceptance receipt;
  it does not replay the old commit/finish transaction.

This is continuation from a verified implementation boundary, not arbitrary
Python instruction replay. Checks and later phases intentionally execute again.

## Preconditions and rejected recovery

Before business work, the host checks repository identity, frozen definitions,
dependency receipts, report hashes, effective config, prompt contents and quality
commands. Tracked/nonignored untracked content, file modes, symlinks, HEAD and
Git index entries must match the terminal checkpoint, including spec documents.
Existing partial changes therefore need no temporary commit, while unrelated
edits made after the stop cause `preflight_rejected` (exit 2), without mutation.
Neither mode silently adopts changed workspaces or changed policies.

Recovery requires a `stopped` checkpoint. Successful/live runs, legacy runs with
no checkpoint, planning/decomposition-only failures, standalone rechecks and
SIGKILL/crashes without a saved terminal checkpoint are not eligible. Report the
specific rejection and preserve the workspace; do not fabricate a checkpoint,
erase edits or mark an unknown outcome successful. Submodule/special-file
workspaces are rejected because this checkpoint format cannot verify them.

The trace schema remains version 2. Checkpoints have their own version 1 schema;
there is no historical session importer. See the [upgrade procedure](../cookbooks/install.md#upgrading-delivery-recovery)
for preserved user-owned entrypoints.
