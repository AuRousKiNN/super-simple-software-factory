# Specification and execution artifacts

The authoritative layout is `specs/<spec_key>/spec.md`, with optional
`spec.tickets/`, a cumulative `README.md`, and immutable
`executions/<adw_id>/<execution_id>.md`. Runtime owns `specs/README.md`.
The launching agent chooses the stable directory; runtime validates paths,
symlinks and conflicts. New planner calls require `PlanningTarget(spec_dir=...)`;
revision calls use `PlanningTarget(spec=...)`. CLI equivalents are `--spec-dir`
and `--spec`, mutually exclusive. Revision keeps the same file, increments its
frontmatter revision and uses a new session after an implementation binding.

A business ADW calls `spec_artifacts.document(run, DocumentRequest(...))` with
an explicit root `SpecWorkItem` or `TicketWorkItem`, purpose, captured changes,
checks, review and hashed evidence. Missing references or hash drift fail closed.
No code diff is required for new evidence or blockers. To return no change when
there are no facts and no explicit record request, set `record_requested=False`.

The helper runs input collection, a documenter AgentCall with `DocumentContext`,
and host publication. Agents return `DocumentDraftOutput` pointing to two bodies
in the current invocation reports directory. Host publication yields
`DocumentOutput` with `spec_path`, `document_path`, `overview_path` and actual
formal artifacts. JSON/gate retries keep the same execution ID; independent
calls get unique IDs, even within the same session and phase name.

The README separates implementation and verification. Its frontmatter and top
facts show definition, observation time, checked commit/content snapshot, scope,
acceptance provenance and known limitations. Body text is cumulative and should
preserve unrelated requirement history while marking evidence needing recheck.
Runtime supplies navigation; draft bodies must not include managed metadata.
Links inside draft bodies must be relative to their formal publication positions.

## Finish and acceptance

Documenter success, workflow acceptance and scope acceptance are distinct.
Use `prepare_finish(run, document)` before `run.finish()` to persist pending fact
projection. Only an ADW owning complete acceptance of the observed scope may pass
an explicit host review receipt using `FinishOptions(receipt=..., accepts_scope=True)`. The complete SDLC does
so; generated workflows and recheck do not imply full integration acceptance.
Ticket acceptance remains `ticket-acceptance.json`; a ticket never accepts the
whole spec. Evidence, definitions and snapshots must match the declared scope.

Finish stores the real result, releases the original lock, then reacquires it to
compare the expected README and definitions. It never invokes another Agent or
calls finish twice. A failed projection returns a nonzero command result while
preserving the real workflow result and pending recovery state. Set `FinishOptions.commit=True`
only for ADWs that already commit; post-finish commits contain only managed facts
and the index. Documentation commits do not change the observed implementation.

## Recover without another agent

```bash
uv run adws/spec_artifacts_cli.py index
uv run adws/spec_artifacts_cli.py recover --adw-id SESSION --execution-id EXECUTION
uv run adws/spec_artifacts_cli.py sync --adw-id SESSION
```

Publication journals are host-owned session files under `spec-artifacts/`.
States distinguish drafts, prepared publication, published report, overview and
completed publication. A journal is recoverable progress, not an atomic
multi-file transaction. The report is never overwritten. Replay of equal content
is idempotent; different content or a changed README is a conflict requiring
explicit reconciliation. Draft-only attempts need a later business workflow;
recovery never invents a successful agent output.

`spec-finish.json` stores the pending projection, actual finish result and errors.
`spec-acceptance/<execution_id>.json` preserves each explicit scope acceptance
receipt, and the overview links that host result with its content hash.
`sync` replays these facts without reissuing acceptance. Unknown finish state is
an error. Resolve README conflicts against both observations; never choose the
last writer mechanically. No independent documentation ADW is provided.

Index refresh detects definition/snapshot drift and incomplete publication. It
can rebuild navigation from current managed artifacts without importing sessions
into SQLite. Static files describe the last observation and do not update while
no workflow or refresh command is running. Missing historical logs limit evidence
reuse; Markdown summaries are not original acceptance evidence.

## Ticket recheck records

Ticket-mode `adw-recheck` publishes its execution report and overview under the
canonical specification directory and refreshes `specs/README.md`. Finish replaces
the bound scope's acceptance with the current verdict and receipt, including failed
rechecks, while preserving historical execution reports. Successful ticket rechecks
commit only the published documentation and issue `ticket-acceptance.json` against
the final HEAD. Prerequisite evidence need not share one exact HEAD; documentation
commits do not change implementation content. Ticket acceptance never implies
whole-spec integration acceptance. Spec-mode recheck also projects the actual finish
verdict and explicit review receipt for its whole-spec scope.
