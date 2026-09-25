# Update runtime modules

Low-level behavior belongs in `adws/adw_modules/`. Keep ADW entry points thin.

| Module | Responsibility |
|---|---|
| `data_types.py` | strict config, envelopes, runtime requests/results, events, usage |
| `agents.py` | config merge, prompts, structured turns, repair budgets, gates |
| `agent_codex.py` | SDK clients, preflight, thread/turn lifecycle, cancel and close |
| `codex_events.py` | notification normalization, tool and child tracking, usage deltas |
| `codex_schema.py` | verified Pydantic-to-runtime schema subset |
| `spec_artifacts.py` | spec identity, dual-draft publication, facts, recovery and indexes |
| `tickets.py` | planning metadata, indexes, evidence, work-item binding |
| `evidence_freshness.py` | explicit recheck prior-evidence investigation |
| `permissions.py` | content snapshots, write contracts, safe restoration |
| `runner.py` | phase lifecycle, atomic thread mapping, totals, finish verdict |
| `recovery.py` | host-owned delivery checkpoints, exact workspace validation, explicit new attempts |
| `session.py` | session creation, recovery, signals, runtime shutdown |
| `tracer.py` | schema-v2 SQLite persistence |
| `review_routing.py` | structured ownership, bounded route decisions, immutable handoffs and recheck validation |
| `quality.py` | repository-specific deterministic commands |

Rules for changes:

1. SDK types stay behind `agent_codex.py`; ADWs consume SSSF data types.
2. Preserve events and reported usage on failed or interrupted turns.
3. Run permission verification from `finally`-equivalent terminal handling.
4. Treat thread IDs and turn IDs as runtime-issued identifiers.
5. Persist a new thread mapping immediately after creation and a turn ID when
   the turn starts.
6. Keep unknown events in raw output and fail closed on an unknown success
   terminal.
7. Do not infer cost, child attribution, or context occupancy.
8. Schema/database changes need an explicit current-version migration policy;
   never overwrite an unknown schema.
9. Update distribution templates and tests together.

Run all deterministic tests after a module change, and build the visualizer when
the event or storage contract changes.
