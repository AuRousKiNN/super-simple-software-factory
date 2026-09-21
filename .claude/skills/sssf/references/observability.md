# Observability Reference

The Codex event schema, the versioned SQLite tables, and the polling contract — the one data path is **agents → sqlite → web ui**.

## Two stores, one truth

**Files are the raw record** (`raw_output.jsonl` streams, `envelope.json`, `agent_map.json`); **SQLite (`sssf.db`) is the queryable mirror** the UI reads. `tracer.py` writes both. Losing the db loses nothing that can't be rebuilt from files.

Location comes from `observability.db` in `sssf.config.yaml`, default `adws/adw_data/sssf.db` — inside the **target** repo, gitignored.

## Event schema

`tracer.py` emits these types, every one logged against its `adw_id` **and** `phase_id`:

| Type | Emitted when |
|---|---|
| `phase_start` | a `run.phase(...)` block is entered |
| `agent_start` | a coding agent is spawned or resumed for `ph.call(...)` |
| `tool_call` | a Codex command, file change, MCP call, or dynamic tool terminates — **one event per item**, deduplicated by invocation/thread/turn/item; interrupted tools are explicit failures |
| `subagent_start` | an allowed child thread starts; payload links parent/child thread and turn IDs plus the custom role |
| `subagent_end` | a child reaches `completed` or `interrupted` |
| `subagent_result` | Codex reports a child's terminal status, result or error through `agentsStates` |
| `subagent_log` | a non-terminal child interaction is observed |
| `handoff` | an envelope crosses from one agent to the next |
| `gate_pass` | a gate found no failed checks — payload carries `attempt`, `checks` (the evidence), and an empty `violations` |
| `gate_fail` | a gate found at least one failed check — payload carries `attempt`, `checks`, and `violations` |
| `log` | an explicit `ph.log(...)` from the ADW script |
| `agent_end` | the agent's run completes; envelope parsed or not — payload carries `cost`, `usage`, context metrics, all observed child records, child-usage attribution, and whether cleanup had to close the owning runtime |
| `phase_end` | the block exits; carries the resolved status |
| `error` | a raise inside a phase block |

`parent_id` nests spans, so an agent phase expands into its tool-call spans in the UI.

**Usage follows schema v2.** `input_tokens` already includes `cached_input_tokens`, and `reasoning_tokens` is already a subset of `output_tokens`; neither is added to `total_tokens` again. `uncached_input_tokens` is present only when the input/cache fields are complete. Retries and failed turns still settle usage. A unique thread/turn settlement prevents resume or duplicate notifications from billing the same turn twice.

**Unknown cost is not zero.** The current runtime does not provide a reliable amount, so turns record `cost=null, cost_kind=unknown`. `sessions.total_cost` is the known subtotal and `cost_complete` says whether it is a complete total. The terminal and UI show `unknown` whenever completeness is false.

**Context is independent of spend.** Thread cumulative token usage is billing history, not current window occupancy. Until Codex provides an explicit occupancy measurement, `context_tokens` remains NULL and the lane omits its progress bar. `context_window` may still be known, but is never paired with cumulative spend to invent a percentage.

**Child usage is never guessed or double-counted.** Child lifecycle and any child-thread usage notification are attached to the parent `agent_end` record. `child_usage_attribution` is `separate` only when every child published its own usage; otherwise it is `unknown`. Child usage is not added to the parent's reported turn usage because the locked runtime does not establish whether the parent total already includes it.

**Gates record evidence, not just a verdict.** A gate returns one `{item, ok, note}` check per thing it looked at, and `violations` are derived from the failed ones. Both land in `gate_results` (`checks_json` + `violations_json`) and in the `gate_pass`/`gate_fail` payload, so a green gate can answer *what did you verify* — `{"item": "…/plan.md", "ok": true, "note": "exists, 454B"}` — rather than only *did it pass*. Rows written before this existed have `checks_json` NULL; treat that as "no evidence recorded", not "nothing checked".

The gate event payload carries `attempt` too, so the `gate_results` table and the event stream are equivalent sources — a live consumer can group gate results per correction round from events alone, without a second query.

**A `tool_call` is the one event that spans time**, so it fills both `started_at` and `ended_at` on the row — the tool's real start and return. Every other type is a point in time: `started_at` is when it was recorded and `ended_at` stays NULL. Lay tool calls out on a time axis from those columns, never by parsing `payload_json` (`duration_ms` is in the payload too, as pi's own number, but it is a convenience, not the source for layout).

**Streaming is solved by construction.** The Codex SDK notification stream is serialized to `raw_output.jsonl`; normalized terminal tool events are inserted into `sssf.db` while the turn is running. Unknown notifications stay in the raw stream and increment a diagnostic counter. Everything downstream is a poll → render.

## Tables

```sql
schema_meta (
  singleton      INTEGER PRIMARY KEY,
  schema_version INTEGER             -- exactly 2
);

sessions (
  adw_id        TEXT PRIMARY KEY,
  request       TEXT,              -- the engineer's ask
  status        TEXT,              -- running | success | fail
  engineer      TEXT,
  started_at    TEXT, ended_at TEXT,
  total_tokens  INTEGER,
  total_cost    REAL,               -- known subtotal
  cost_complete INTEGER             -- 0 when any amount is unknown
);

phases (
  phase_id      TEXT PRIMARY KEY,
  adw_id        TEXT REFERENCES sessions,
  seq           INTEGER,
  name TEXT, kind TEXT, owner TEXT, description TEXT,
  status        TEXT DEFAULT 'fail',   -- success must be earned
  attempt       INTEGER DEFAULT 0, retries INTEGER DEFAULT 0,
  error         TEXT,
  started_at    TEXT, ended_at TEXT
);

events (
  event_id      TEXT PRIMARY KEY,
  adw_id        TEXT REFERENCES sessions,
  phase_id      TEXT REFERENCES phases,   -- every event logs against adw + phase
  parent_id     TEXT,                     -- span nesting
  type          TEXT,   -- phase_start | phase_end | agent_start | agent_end | tool_call
                        -- | subagent_start | subagent_end | subagent_result | subagent_log
                        -- | handoff | gate_pass | gate_fail | log | error
  name          TEXT,
  payload_json  TEXT,
  tokens        INTEGER,
  started_at    TEXT, ended_at TEXT   -- ended_at set only on events that span time
);

envelopes (
  envelope_id   TEXT PRIMARY KEY,
  adw_id        TEXT REFERENCES sessions,
  phase_id      TEXT REFERENCES phases,
  agent         TEXT,
  output_type   TEXT,              -- name of the data_types model it parsed against
  payload_json  TEXT,
  valid         INTEGER,
  attempt       INTEGER,
  created_at    TEXT
);

gate_results (
  id            INTEGER PRIMARY KEY,
  adw_id        TEXT REFERENCES sessions,
  phase_id      TEXT REFERENCES phases,
  attempt       INTEGER,
  gate          TEXT,
  passed        INTEGER,
  violations_json TEXT,             -- derived: the failed checks, as "item: note"
  checks_json   TEXT,               -- [{item, ok, note}] — everything the gate looked at
  created_at    TEXT
);

processes (                        -- adw_id → pid, so a stuck run can be stopped
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  adw_id        TEXT REFERENCES sessions,
  kind          TEXT,               -- 'adw' (the workflow process) | 'agent' (a coding-agent child)
  name          TEXT,               -- '' for the adw, the agent name for a child
  pid           INTEGER,
  start_marker  TEXT,               -- start identity; protects against PID reuse
  command       TEXT,
  started_at    TEXT, ended_at TEXT -- ended_at NULL = believed alive
);

agent_sessions (                   -- the queryable mirror of agent_map.json
  adw_id        TEXT REFERENCES sessions,
  agent         TEXT,
  coding_agent  TEXT, model TEXT, color TEXT,   -- color: the config's lane swatch
  session_id    TEXT,                -- Codex thread id (column name retained for UI contract)
  context_tokens INTEGER,           -- NULL until runtime reports real occupancy
  context_window INTEGER,
  created_at    TEXT, last_used_at TEXT,
  PRIMARY KEY (adw_id, agent)
);

agent_invocations (
  invocation_id TEXT PRIMARY KEY,
  adw_id TEXT, phase_id TEXT, agent TEXT,
  thread_id TEXT, turn_id TEXT, status TEXT,
  sdk_version TEXT, runtime_version TEXT,
  usage_json TEXT, usage_settled INTEGER,
  started_at TEXT, ended_at TEXT
);
```

The active database must contain `schema_meta.schema_version = 2` and every v2 table. Older or incomplete databases are rejected with an explicit backup-and-recreate message; the runtime never mutates a pre-Codex database in place.

**A hung agent emits nothing**, which is exactly when you need its pid. The runtime records each owned app-server child with PID, command, and start marker; shutdown checks the identity before TERM/KILL so a recycled PID is never targeted. `Codex.close()` gets a grace period, after which owned children are terminated. A killed run finalizes its own trace: SIGTERM and SIGINT become `SystemExit`, so the session lands on `fail` with process rows closed.

**Derived, never stored:** phase durations (`ended_at − started_at`), session phase-progress (query `phases` by `adw_id`), lane layout (`kind` + `owner`).

Phase status invariants: `queued` only for manifest-declared phases not yet entered (dashed in the UI); `running` on enter; only a clean exit writes `success` — agent phases additionally need the envelope parsed and gates green; everything else resolves to `fail`.

## WAL pragmas

Open **every** connection — writer and reader — with:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
```

WAL allows readers during writes. Writers are the tracers of running ADW processes; concurrent writers are fine given one small transaction per event plus `busy_timeout`. The visualizer reads on a readonly connection with exactly one exception: archiving a session (`POST /api/sessions/:adw_id/archive`) opens a second connection to set `sessions.archived`. That flag is review triage — it says a human has looked at the run — so it is the reader's state living on the row, and no tracer ever writes or reads it.

## Polling contract

**The UI never receives pushes.** No ingest endpoint, no WebSocket, no backfill or dedup logic.

Live view polls on a rowid cursor every `observability.poll_ms` (default 500):

```sql
SELECT ... FROM events WHERE adw_id = ? AND rowid > ? ORDER BY rowid LIMIT 500;
```

Keep the highest `rowid` returned as the next cursor. History is **the same queries** with filters, lazy-paged as the engineer scrolls or drills in — one mechanism serves both live and past runs, which is why there is no separate replay path.
