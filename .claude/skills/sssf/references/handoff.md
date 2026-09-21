# Handoff contract

Context crosses a phase boundary through two channels:

1. a typed envelope returned as the successful turn's final result;
2. explicit artifacts under the session's `context_handoff/` directory.

Commentary, reasoning summaries, tool output, and partial messages are never an
envelope. A turn containing valid-looking JSON still fails when its terminal
status is not `completed`.

## Envelope lifecycle

The call site selects an `EnvelopeBase` subtype. SSSF generates a strict output
schema for every turn, including correction turns, then validates the final
payload with Pydantic. Unknown fields are rejected.

When parsing fails, the invalid output is persisted separately and the same
thread receives a bounded correction. A valid envelope is written to the
invocation directory and copied to `<role>/envelope.json` as the latest-valid
compatibility view. Failed output never replaces that view.

Gate violations follow the same-thread correction path. Permission violations
do not: they terminate the phase immediately after restoration.

## Session mapping

`sessions/<adw_id>/agent_map.json` uses schema version 2. Each role entry records
the backend literal, runtime-issued thread and turn IDs, model, runtime version,
config fingerprint, repository root, state, and cumulative-usage baseline when
available.

Resume requires every compatibility field to match. A non-current mapping,
missing thread, changed role instructions, changed sandbox/tool policy, or
changed model fails explicitly. There is no implicit last-thread lookup or cold
start.

## Invocation files

```text
<role>/invocations/<invocation_id>/
├── request.json
├── prompts/system.md
├── prompts/user.md
├── output.schema.json
├── raw_output.jsonl
├── runtime.stderr.log
├── reports/
└── envelope.json
```

SDK notifications are serialized with receipt time; they are not described as
wire-byte captures. Secrets and authentication messages must not enter these
files.

## Failure handoff

Authentication, model, effort, approval, timeout, interruption, runtime,
unknown-outcome, and child-policy failures are distinct. They preserve received
events and usage, run write verification, and close the invocation. A crash
whose side effects cannot be determined becomes `outcome_unknown` and requires
explicit recovery before another mutating turn.
