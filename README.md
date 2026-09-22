# Super Simple Software Factory

> Repeatable agent-and-code workflows packaged as one installable skill.
> Deterministic Python owns the graph; Codex is a bounded runtime inside it.

SSSF turns a request into an observable AI Developer Workflow (ADW). Python
controls phase ordering, retries, write boundaries, deterministic checks, Git
commits, and final acceptance. Agents receive role-specific instructions and
return typed JSON envelopes. Every invocation, gate, tool call, child agent,
usage update, and failure is persisted to SQLite.

The distribution has one agent runtime: the official Python Codex SDK,
`openai-codex==0.155.1`.

## 项目交互导览

用浏览器打开 [中文交互导览](docs/project-guide/index.html)，从项目全貌逐步了解交付流程、角色权限、验收机制与源码结构。导览可离线使用，内含失败情境演示与源码链接，不会执行工作流。

## Install

Prerequisites:

- Python 3.10+ and [`uv`](https://docs.astral.sh/uv/)
- Codex CLI authentication (`codex login status`)
- Git and SQLite
- `just` for the included recipes, optional
- Bun only for the visualizer

Copy `.agents/skills/sssf/` into the target repository, then run from the target
root:

```bash
uv run .agents/skills/sssf/scripts/install.py
git init
just demo
```

The installer writes a file-digest manifest at `.sssf/manifest.json` and a
pre-install snapshot under `.sssf/backups/`. A repeated install is a no-op when
the target is current.

User-owned config, prompts, starter workflows, `quality.py`, `.env.sample`, and
`justfile` are preserved. Managed runtime files update automatically only when
their current digest matches the previous manifest. A locally changed managed
file stops the install before any write:

```bash
# After reviewing the reported conflict; the update creates a pre-write backup:
uv run .agents/skills/sssf/scripts/install.py --force-managed

# Stop workflows first; rollback refuses if captured files changed afterwards.
uv run .agents/skills/sssf/scripts/install.py --rollback latest
```

`--force` is retained as an alias for `--force-managed`; it never overwrites
user-owned files. Installation is transactional: a failed write restores every
target touched by that attempt. Snapshots include the active config and SQLite
database files so a verified Codex distribution can be rolled back with its
matching runtime state.

## What is installed

| Target | Purpose | Ownership |
|---|---|---|
| `adws/adw_modules/` | runtime, gates, tracing, permissions, quality helpers | managed, except `quality.py` |
| `adws/adw_*.py` | starter workflows | user-owned |
| `adws/adw_sssf_config/sssf.config.yaml` | strict schema-v2 roster | user-owned |
| `adws/adw_data/prompt_engineering/` | role prompts | user-owned |
| `.codex/agents/sssf_recon.toml` | read-only child-agent role | managed |
| `.env.sample` | optional API-key mode example | user-owned |
| `justfile` | run and observe recipes | user-owned |
| `.sssf/manifest.json` | installed file digests and distribution version | installer state |

Runtime sessions and the trace database live under `adws/adw_data/` and are
gitignored. A fresh installation does not require any legacy runtime, provider
catalog, compatibility config, or historical session data.

## Architecture

```text
ADW script / PhaseHandle.call
  -> role prompt + per-turn output schema
  -> CodexRuntime (thread, turn, stream, cancel, close)
  -> normalized events and AgentRunResult
  -> permission verification
  -> Pydantic envelope + gates
  -> next phase or bounded correction on the same thread
```

The separation is deliberate:

- `agents.py` owns config resolution, prompt rendering, bounded repair loops,
  permissions, gates, and envelopes.
- `agent_codex.py` owns the SDK lifecycle, thread/turn operations, preflight,
  cancellation, and runtime errors.
- `codex_events.py` normalizes SDK notifications and usage.
- `permissions.py` snapshots and restores content, modes, symlinks, and the Git
  index when an agent crosses its write contract.
- `tracer.py` persists schema-v2 records without interpreting runtime protocol.

One `adw_id + role` maps to one Codex thread when the saved config fingerprint
still matches. JSON repairs and gate corrections are additional turns on that
thread. A missing, incompatible, or non-current mapping fails explicitly; it
does not silently start a replacement conversation.

## Configuration

The installed config is strict: unknown keys fail validation.

```yaml
schema_version: 2

defaults:
  coding_agent: codex
  model: gpt-5.6-terra
  thinking: medium
  data_dir: adws/adw_data
  protected_files:
    - adws/adw_modules/
    - adws/adw_sssf_config/
    - adws/adw_*.py
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
  - name: scout
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/scout/system.md
      user: adws/adw_data/prompt_engineering/scout/user.md
    subagents:
      enabled: true
    writes: []
```

`thinking` is validated as a Codex reasoning effort. `writes` is a content
contract, separate from the Codex sandbox:

- omitted or `null`: any repository path except `protected_files`
- `[]`: no repository modifications may remain
- a list: only matching paths may remain

The current invocation's report directory and `context_handoff/` are narrow
runtime exceptions. Being under `data_dir` does not otherwise grant write
permission.

CLI authentication is the default and needs no secret in `.env`. For unattended
execution, select `codex.auth: api_key` and set `OPENAI_API_KEY`. Secrets are not
written into prompts, traces, config snapshots, or process arguments. A custom
`CODEX_PATH` must point to a deliberately verified runtime compatible with SDK
0.155.1.

## Agents, envelopes, and gates

The starter roster contains `planner`, `builder`, `scout`, `reviewer`, and
`documenter`. There is no tester agent: a known command belongs in a deterministic
`kind="code"` phase.

Every agent call declares an `EnvelopeBase` subclass. The runtime derives a
strict JSON schema for every turn, then performs three checks in order:

1. the turn terminal status is `completed`;
2. the final result validates against the declared Pydantic type;
3. write verification and business gates pass.

Invalid JSON and gate violations receive bounded corrections on the same
thread. Authentication, model, approval, timeout, interruption, and runtime
failures are not counted as JSON retries. A phase has a total turn limit.

The output contract is a synchronized triad: the data type, the role prompt's
report example, and the call site's `output_type=`. Change all three together.

## Child agents

Only planner and scout enable child agents in the starter config. The installed
`sssf_recon` role is read-only, disables recursive child creation, and disables
the SSSF orchestrator skill. A parent may run at most six children, must give
each a bounded investigation task, and must wait for all children to terminate
before final acceptance.

Parent/child thread relationships, status, result, errors, and visible usage are
traced. Child usage is never added to parent usage unless the runtime exposes a
provably separate counter. Cancelling a parent closes unfinished children.

## Included workflows

```bash
just prompt "summarize this repo"
just scout "where is authentication handled?"
just plan "add a health endpoint"
just plan-build "implement the approved plan"
just sdlc "plan, build, and test the change"
just simple-sdlc "run plan, build, test, review, and document"
```

Generate a thin workflow from the configured roster:

```bash
uv run .agents/skills/sssf/scripts/make_adw.py \
  --name review_docs --agents scout,reviewer
```

Generated scripts pin the SDK, include `rich`, call `run.finish()`, and use a
concrete output type for each built-in role. Replace the generated phase
descriptions with task-specific intent before relying on the trace.

## Observability

The tracer writes SQLite directly in WAL mode; readers poll by rowid. There is
no second ingestion path. Schema v2 rejects an older database rather than
overwriting or converting it.

```bash
just sessions
just phases <adw_id>
just tail <adw_id>
just procs <adw_id>
just obs
```

Each invocation stores redacted request metadata, rendered prompts, output
schema, serialized SDK notifications, stderr, failed outputs, and the latest
valid envelope. Unknown runtime notifications remain in raw output and raise a
diagnostic counter.

Usage follows Codex semantics: cached input is already part of input tokens,
and reasoning tokens are already part of output tokens. Unknown cost and
context occupancy remain unknown; the UI never displays them as zero.

## Customize first

Before trusting a write workflow:

1. Replace placeholder commands in `adws/adw_modules/quality.py`.
2. Adapt role prompts under `adws/adw_data/prompt_engineering/`.
3. Review models, efforts, subagents, and write contracts in the config.
4. Copy the closest ADW and edit its phases and gates.
5. Commit the installation so installer conflicts and application changes are
   easy to review.

The factory runs on the current checkout and intentionally does not create a
branch or merge automatically. Codex sandboxing and SSSF write verification are
defense-in-depth, not a substitute for repository isolation.

## Development verification

The deterministic suite uses the locked SDK dependency:

```bash
uv run --with openai-codex==0.155.1 --with pytest --with pydantic \
  --with pyyaml --with python-dotenv --with rich \
  python -m pytest -q tests
```

The visualizer is verified separately:

```bash
cd .agents/skills/sssf/apps/visualizer
npm run build
npm run lint
```

See `docs/pi-to-codex-design.md` for the migration rationale and
`docs/codex-sdk-m4-report.md` for the distribution-cleanup acceptance record.

## License

MIT. See [LICENSE](LICENSE).
