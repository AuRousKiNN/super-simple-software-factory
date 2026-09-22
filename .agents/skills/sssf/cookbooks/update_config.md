# Update the roster

Edit `adws/adw_sssf_config/sssf.config.yaml`, then run a workflow that requires
the changed role so `agents.validate()` and runtime preflight exercise it.

## Model and reasoning

`model` is a Codex model ID. `thinking` accepts:

```text
none | minimal | low | medium | high | xhigh | max | ultra
```

Availability is model- and account-dependent. Unsupported combinations fail
before the business turn; SSSF never silently lowers the effort.

## Prompts and identity

Each role needs one `system.md` and one `user.md`. Role instructions are passed
as developer instructions on both thread creation and resume. Task text and the
previous envelope are rendered into the user prompt.

When an output type changes, update its Pydantic model, every prompt report
example, and every `output_type=` call site together.

## Write contracts

`writes` controls what modifications may remain in the repository:

- omit it or set `null` for all paths except `protected_files`;
- use `[]` for read-only repository access;
- list exact paths, directory prefixes, or globs for a constrained writer.

Sandbox configuration and this post-run content check are separate controls.
The current invocation report directory and `context_handoff/` are the only
automatic runtime exceptions.

## Child agents

Only planner, scout and decomposer may opt in:

```yaml
subagents:
  enabled: true
  max_concurrent: 6
  role: sssf_recon
  config_file: .codex/agents/sssf_recon.toml
```

The role file must live under `.codex/agents/`, use a read-only sandbox, disable
recursive child creation, and disable the SSSF skill. Keep child agents disabled for builder, reviewer and documenter. Decomposer
children investigate read-only; the parent owns planning writes.

## Authentication and network

Use `codex.auth: cli` for an operator session. For unattended execution choose
`api_key` and provide `OPENAI_API_KEY` outside the YAML. Command network access
is disabled in the current release and `approval_policy` is non-interactive.
