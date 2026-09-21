# Create the roster

Prefer the generator:

```bash
uv run .claude/skills/sssf/scripts/make_config.py
```

It writes the strict schema-v2 starter config and the project-scoped
`sssf_recon` child role. It refuses to overwrite an existing config unless the
engineer explicitly passes `--force`.

Minimal shape:

```yaml
schema_version: 2

defaults:
  coding_agent: codex
  model: gpt-5.6-terra
  thinking: medium
  data_dir: adws/adw_data
  subagents:
    enabled: false
    max_concurrent: 6
    role: sssf_recon
    config_file: .codex/agents/sssf_recon.toml

codex:
  auth: cli
  approval_policy: never
  turn_timeout_s: 900
  startup_timeout_s: 30
  shutdown_grace_s: 10
  command_network_access: false

agents:
  - name: builder
    purpose: Implement the requested change and report every modified file.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/builder/system.md
      user: adws/adw_data/prompt_engineering/builder/user.md
```

Use a direct Codex model ID. The runtime preflights model and reasoning-effort
support. Do not add provider catalog syntax or a backend fallback.

Every config object rejects unknown keys. Lists replace inherited lists; an
explicit `writes: []` is read-only, while omitted or `null` means unrestricted
except for `protected_files`.
