# SSSF overview

SSSF is a Python control plane around the official Codex SDK. An ADW is a thin
script made of named phases. A phase belongs to the engineer, an agent, or a
deterministic code step. Typed envelopes and files in `context_handoff/` are the
only supported handoff channels.

```text
adws/
├── adw_*.py                         workflow entry points
├── adw_modules/
│   ├── agents.py                    config, prompts, retries, gates
│   ├── agent_codex.py               SDK lifecycle and turns
│   ├── codex_events.py              event and usage normalization
│   ├── permissions.py               content snapshots and restoration
│   ├── tracer.py                    SQLite schema-v2 persistence
│   └── runner.py / session.py       ADW lifecycle and recovery
├── adw_sssf_config/sssf.config.yaml strict schema-v2 roster
└── adw_data/
    ├── prompt_engineering/           user-owned role prompts
    ├── sessions/                     runtime records, gitignored
    └── sssf.db                       WAL trace database, gitignored
```

The installed `.codex/agents/sssf_recon.toml` role permits planner and scout to
delegate bounded read-only investigations. Child creation is disabled inside
that role, so delegation cannot recurse.

Key invariants:

- `agents.validate()` runs before a business turn.
- every call declares an output type and receives a strict per-turn schema;
- repairs continue the same thread;
- write verification runs on every terminal path;
- unknown cost and context remain unknown;
- every ADW exits through `run.finish()`.

Use the other cookbooks for an operation. Read deep references only when their
subject is needed.
