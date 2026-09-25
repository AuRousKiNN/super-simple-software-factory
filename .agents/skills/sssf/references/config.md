# Configuration reference

The root is a strict `schema_version: 2` object with `defaults`, `codex`,
`observability`, and `agents`. Unknown fields at any level are errors.

## Defaults and agent fields

| Field | Type | Meaning |
|---|---|---|
| `coding_agent` | literal `codex` | fixed runtime identifier |
| `model` | string | direct Codex model ID |
| `thinking` | reasoning effort | preflighted against the selected model |
| `color` | string | visualizer lane color |
| `subagents` | object | child policy; disabled by default |
| `writes` | null or list | repository content contract |
| `protected_files` | list | paths agents cannot modify unless explicitly allowed |
| `data_dir` | string | sessions and trace runtime root |

Known scalar/object defaults are inherited when absent on an agent. `writes: []`
is not treated as absent. Nested `subagents` fields merge by known key; lists
replace rather than append.

## Codex runtime

| Field | Values/default | Meaning |
|---|---|---|
| `auth` | `cli` or `api_key` | operator login or unattended key mode |
| `approval_policy` | `never` | no interactive elevation prompt |
| `turn_timeout_s` | `2400` | total turn deadline |
| `startup_timeout_s` | `30` | runtime startup deadline |
| `shutdown_grace_s` | `10` | cancellation/close grace period |
| `command_network_access` | `false` | command network remains disabled |

`approval_policy: never` does not disable the sandbox. An operation requiring
more authority returns `approval_required` instead of waiting for input.

## Reasoning effort

Accepted values are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`,
and `ultra`. Acceptance by the schema is not a promise that a model supports the
value. Preflight checks the locked runtime's model metadata.

## Write semantics

Patterns are normalized relative to the repository root. Absolute paths,
parent traversal, and escapes through symlinks are rejected. Directory entries
end in `/`; glob patterns use standard matching.

The runtime data directory is not globally writable. The host writes config
snapshots, mappings, SQLite, prompts, and raw notifications. Agents may leave
only the current invocation report and declared context-handoff artifacts there.

## Child policy

`enabled`, `max_concurrent`, `role`, and `config_file` are the only fields.
`max_concurrent` is 1–6. The starter distribution permits opt-in only for
planner, scout and decomposer. The role file is validated before any business turn.

## Observability

`observability.db` defaults to `adws/adw_data/sssf.db`; `poll_ms` defaults to
500. The tracer accepts only its current schema. Back up a non-current database
outside the active data directory before starting a new one.
