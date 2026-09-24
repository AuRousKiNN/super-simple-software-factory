# Run and monitor an ADW

Read [how_to_prompt_for_the_eng.md](how_to_prompt_for_the_eng.md) first. Launch
the workflow the engineer named, or select an installed chain by reading each
`adw_*.py` `Phases:` line. Do not perform a phase's application work yourself.

## Launch

```bash
uv run adws/adw_simple_sdlc.py "add a health endpoint" --spec-dir specs/health-endpoint
uv run adws/adw_scout.py requests/investigate.md
uv run adws/adw_build_test.py "implement the accepted plan" --adw-id a1b2c3d4
uv run adws/adw_plan.py "plan the change" --spec-dir specs/example --config path/to/roster.yaml
```

An inline prompt and a prompt-file path are equivalent. `--adw-id` joins a
schema-v2 session when its role mapping remains compatible; it does not perform
an implicit last-session lookup. A changed model, role instructions, runtime,
repository root, or permission policy makes resume fail explicitly.

If the engineer names a roster or model tier, resolve it to a config file and
pass it. Do not swap rosters on your own: model selection affects cost, context,
and compatibility.

Launch in a way that keeps the process output available, record the printed
`adw_id`, then monitor SQLite rather than repeatedly inspecting application
files.

## Monitor

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
emitting events. Check the current phase and owned process identity. Termination
must go through the workflow's normal signal path so the active turn is
cancelled, child agents are closed, permission verification runs, and process
rows settle. Never kill a PID without verifying its saved start marker because
PIDs can be reused.

Timeout, interruption, authentication, model, approval, runtime, and
unknown-outcome failures have different recovery implications. Report the exact
kind rather than relabeling all of them as a failed envelope.

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

## Choose the specification target

The launching agent decides new versus existing work before invoking a planner.
Select an unused readable `specs/<spec_key>` and pass `--spec-dir`; for revision,
pass `--spec specs/<spec_key>/spec.md` in a new session. Do not ask the user to name
routine directories. Missing targets and collisions fail before a planner turn;
resolve them explicitly rather than retrying with automatically renamed paths.
All subsequent work inherits this identity, including ticket implementation.

Read `specs/<spec_key>/README.md` for observed progress and `spec.md` for the goal.
Execution history is under `executions/<adw_id>/`. Never treat document generation
or planning acceptance as whole-spec acceptance. An unsynced/freshness warning
means the visible observation must be reconciled with current evidence.

See [execution artifact recovery](../references/spec-artifacts.md).
