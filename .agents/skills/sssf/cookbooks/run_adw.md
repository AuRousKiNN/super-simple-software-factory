# Run and monitor an ADW

Read [how_to_prompt_for_the_eng.md](how_to_prompt_for_the_eng.md) first. Launch
the workflow the engineer named, or select an installed chain by reading each
`adw-*.py` `Phases:` line. Do not perform a phase's application work yourself.

## Choose the specification target

The launching agent decides new versus existing work before invoking a planner.
Select an unused readable `specs/<spec_key>` and pass `--spec-dir`; for revision,
pass `--spec specs/<spec_key>/spec.md` in a new session. Do not ask the user to name
routine directories. Missing targets and collisions fail before a planner turn;
resolve them explicitly rather than retrying with automatically renamed paths.
This applies to every `adw-plan*` workflow, including `adw-plan-decompose`,
and any other chain containing a planner. Mentioning a directory in the prompt
is insufficient; pass the CLI argument. Decomposition inherits the spec path and
writes `specs/<spec_key>/spec.tickets/`. All subsequent work inherits this
identity, including ticket implementation.

Check the installed script's `--help` before launch. If it lacks these options,
follow the [upgrade instructions](install.md#upgrading-specification-artifacts);
installation preserves existing ADWs, so a skill/runtime update alone does not
migrate their CLI.

Read `specs/<spec_key>/README.md` for observed progress and `spec.md` for the goal.
Execution history is under `executions/<adw_id>/`. Never treat document generation
or planning acceptance as whole-spec acceptance. An unsynced/freshness warning
means the visible observation must be reconciled with current evidence.

See [execution artifact recovery](../references/spec-artifacts.md).

## Launch

```bash
uv run adws/adw-simple-sdlc.py "add a health endpoint" --spec-dir specs/health-endpoint
uv run adws/adw-plan-decompose.py "plan and split the change" --spec-dir specs/example-tickets
uv run adws/adw-plan-decompose.py "revise the plan and tickets" --spec specs/example-tickets/spec.md
uv run adws/adw-scout.py requests/investigate.md
uv run adws/adw-build.py --spec specs/example/spec.md
uv run adws/adw-plan.py "plan the change" --spec-dir specs/example --config path/to/roster.yaml
```

Commit existing planning artifacts before starting delivery. Ticket rechecks keep
HEAD fixed and store their execution documents in the session directory.

An inline prompt and a prompt-file path are equivalent. `--adw-id` joins a
schema-v2 session when its role mapping remains compatible; it does not perform
an implicit last-session lookup. A changed model, role instructions, runtime,
repository root, or permission policy makes resume fail explicitly.

If the engineer names a roster or model tier, resolve it to a config file and
pass it. Do not swap rosters on your own: model selection affects cost, context,
and compatibility.

Launch in a way that keeps the process output available, record the printed
`adw_id`, then wait silently for completion using the process wait facility.
Do not send unsolicited progress updates, repeatedly query SQLite, or inspect
application files while the ADW runs. Answer an explicit user status request
with a targeted read-only check, then return to waiting.

## Inspect status when requested or collect the final result

Use these commands for an explicit status request or to collect the final
report after completion, not as a routine polling loop:

```bash
just sessions
just phases <adw_id>
just tail <adw_id>
just procs <adw_id>
```

Equivalent direct queries:

```bash
sqlite3 adws/adw_data/sssf.db \
  "select seq,name,kind,owner,status,error from phases where adw_id='<id>' order by seq"

sqlite3 adws/adw_data/sssf.db \
  "select rowid,type,name,started_at from events where adw_id='<id>' order by rowid desc limit 25"
```

The terminal and database share the same tracer path. Invocation files under
`adws/adw_data/sessions/<adw_id>/` are the raw record when deeper diagnosis is
requested; never edit them.

## Stalls and termination

A quiet trace can mean the runtime is still starting or a command has stopped
emitting events. Silence alone is not a failure or a reason to interrupt the
ADW. When the user requests diagnosis or termination, check the current phase
and owned process identity. Termination
must go through the workflow's normal signal path so the active turn is
cancelled, child agents are closed, permission verification runs, and process
rows settle. Never kill a PID without verifying its saved start marker because
PIDs can be reused.

Timeout, interruption, authentication, model, approval, runtime, and
unknown-outcome failures have different recovery implications. Report the exact
kind rather than relabeling all of them as a failed envelope.

## Failure: stop and report

A nonzero process exit, a recorded terminal failure, or rejection by
`run.finish(accepted=False, ...)` means the ADW failed. Stop orchestration:
do not launch later ADWs, change code or configuration, or automatically retry,
resume, repair, or recover artifacts. Let the runtime finish its normal cleanup,
collect only the evidence needed for the failure report, then report to the user
and wait for their next instruction. If no phase or `adw_id` was created, report
the launch error and exit code instead. If acceptance cannot be verified, report
that uncertainty rather than claiming success or launching another run.

The ADW's existing bounded gate/JSON retries and fix loops remain internal to
that run. An intermediate failed check inside such a loop is not itself a
terminal ADW failure.

## Report

Tell the engineer:

1. the workflow and non-default config used;
2. the exact prompt sent;
3. the `adw_id`;
4. phase statuses in order;
5. the acceptance result, or the failing phase and error verbatim;
6. whether any permission restoration or forced child/runtime cleanup occurred.

A phase can succeed while reporting a red test result; `run.finish(accepted=...)`
is the authoritative workflow verdict. Do not describe a partial or merely
phase-complete run as accepted.

## Ticket launch preflight

Launch ticket delivery with `--ticket` alone. The host resolves the set and latest
successful current-definition prerequisite receipts; do not manually assemble a
dependency JSON file. See [ticket contract](../references/tickets.md).

`preflight_rejected` (exit 2, no business agent started) is an input rejection, not
an implementation failure. Correct caller-owned arguments and rerun preflight when
already authorized. Never retry unchanged inputs or automatically commit, delete,
ignore user files, change config, or modify acceptance history to pass preflight.
The stop-and-report rule still applies after business execution starts and to
runtime, authentication, permission, interruption and unknown-outcome failures.
