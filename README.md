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

User-owned config, prompts, starter workflows, `quality.py`, and `justfile` are
preserved. Managed runtime files update automatically only when
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
| `adws/adw-*.py` | starter workflows | user-owned |
| `adws/adw_sssf_config/sssf.config.yaml` | strict schema-v2 roster | user-owned |
| `adws/adw_data/prompt_engineering/` | role prompts | user-owned |
| `.codex/agents/sssf_recon.toml` | read-only child-agent role | managed |
| `justfile` | run and observe recipes | user-owned |
| `.sssf/manifest.json` | installed file digests and distribution version | installer state |

The installer gitignores its copied skill, installed runtime, state, Codex role,
and recipes. Specification artifacts under `specs/` remain trackable and are the
only SSSF artifacts intended for the target repository's history. It does not
create an environment-variable example file. A fresh installation does not
require any legacy runtime, provider catalog, compatibility config, or
historical session data.

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
    - adws/adw-*.py
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

The starter roster contains `planner`, `decomposer`, `builder`, `scout`, `reviewer`, and
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

Only planner, scout and decomposer enable child agents in the starter config. The installed
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
just plan "add a health endpoint" --spec-dir specs/health-endpoint
just build --spec specs/health-endpoint/spec.md
just simple-sdlc "run plan, build, test, review, and document" --spec-dir specs/example
```

Generate a thin workflow from the configured roster:

```bash
uv run .agents/skills/sssf/scripts/make_adw.py \
  --name review-docs --agents scout,reviewer
```

Generated scripts pin the SDK, include `rich`, call `run.finish()`, and use a
concrete output type for each built-in role. Replace the generated phase
descriptions with task-specific intent before relying on the trace.

## 工作流入口

所有入口使用连字符，分发包含九个工作流：

| 工作流 | 职责 |
|---|---|
| `adw-prompt` | 单次代理调用 |
| `adw-scout` | 只读侦察 |
| `adw-plan` | 生成或修订规格 |
| `adw-decompose` | 将已有规格分票并生成索引 |
| `adw-plan-decompose` | 规划并分票 |
| `adw-build` | 已有 spec/ticket 或直接请求的完整交付 |
| `adw-quality` | 独立运行适用质量检查 |
| `adw-recheck` | 补证和复核；票据模式重新签发当前基线验收 |
| `adw-simple-sdlc` | 规划后进入与 build 相同的完整交付链 |

`adw-build` 与 `adw-simple-sdlc` 共用 `adw_modules/delivery.py`：
配置预检 → builder → 必跑质量检查 → reviewer → 有限修复与重新验证 → documenter → 提交 → finish。
票据模式随后签发 `ticket-acceptance.json`，签发失败返回非零退出码。
直接文本请求会原样保存到 `specs/request-<adw_id>/spec.md`，提供稳定的审查和文档目标。
执行前要求实现工作区干净；已有规格应采用 `specs/<key>/spec.md` 布局。

质量模板不猜测项目命令。先配置 `quality.check_specs()`；不适用的项目检查通过
`not_applicable_checks()` 声明理由，test 不能排除。缺少必跑命令时在代理调用前停止。
修复后重跑必跑及已执行检查；文档和提交不得改变已验证的实现快照。
规格与票据是目标模式，不存在单独的 ticket 工作流。

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

## Reviewer routing and recheck

Review blockers now carry structured ownership and closure conditions. The two
review workflows distinguish builder repair from execution of configured checks;
planning, environment, required manual and external blockers save a host-owned
handoff and end unaccepted. Generated workflows containing builder use the same complete delivery chain. Check placeholders do not count as passing validation.

Use `uv run adws/adw-recheck.py recheck.json` for an explicit new evidence review
without rebuilding. It validates the original target, current baseline and proof,
then runs required configured checks. See the [routing and recheck contract](.agents/skills/sssf/references/reviewer-routing.md)
for kinds, budgets, request JSON, planning handoffs and existing-install updates.

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

## Optional ticket decomposition

Planner maintains a complete root spec at a stable `PlanOutput.spec_path`.
Decomposer maintains `ticket-set.md` and one Markdown file per ticket under the
spec's `.tickets/` directory. Each planning document starts with `revision: 1`;
revisions update that file in place and increment its number, including wording
changes. Python reads the planning metadata and atomically derives `index.json`. It checks
source binding and dependency relationships, without validating artifact formatting.

```bash
uv run adws/adw-plan-decompose.py "add query and export capabilities" --spec-dir specs/example
uv run adws/adw-decompose.py --spec specs/example/spec.md
uv run adws/adw-build.py --spec specs/example/spec.md
uv run adws/adw-build.py --ticket specs/example/spec.tickets/tickets/TICKET-QUERY.md \
  --ticket-set specs/example/spec.tickets/ticket-set.md
```

Use a new session for each ticket. Repairs retain the same work item and thread;
changed definitions require revalidation and a new session. A ticket with blockers
also needs `--dependency-evidence` referencing host acceptance records. Core accepts
only evidence for the exact current implementation baseline. Use ticket mode in
`adw-recheck` to rerun required checks and review after baseline/environment changes.
`adw-build` performs the complete delivery chain and issues ticket acceptance only
after checks, review, applicable manual evidence, documentation and commits succeed.
See [the ticket contract](.agents/skills/sssf/references/tickets.md) and
[upgrade instructions](.agents/skills/sssf/cookbooks/install.md).

### 规格与执行现状

启动规划时显式选择新目录或已有规格：

```bash
uv run adws/adw-plan.py "规划登录限流" --spec-dir specs/login-rate-limit
uv run adws/adw-simple-sdlc.py "实现登录限流" --spec specs/login-rate-limit/spec.md
```

`spec.md` 定义目标，规格目录中的 `README.md` 汇总最近观察到的实现和验证现状，
`executions/<adw_id>/` 保留每次执行报告。`specs/README.md` 是宿主重建的总索引。
文档生成、工作流成功和完整规格验收分别记录；部分 ticket 完成不会自动验收整个规格。
文稿发布失败可重放，已发布历史不可覆盖。详见
[合同与恢复命令](.agents/skills/sssf/references/spec-artifacts.md)。

先前证据不要求与当前 HEAD 完全一致。多个前置票据可以使用不同提交上的验收记录，
使用先前证据前，由一个只读 scout 简单调查相关代码、测试、配置和环境是否仍适用；
无关改动本身不使证据过时。scout 判定已过时或无法确认时，ADW 直接返回临时调查结果并立即失败，
不进入 builder、质量检查或 reviewer，也不自动补验。builder/reviewer 不重复判断证据新鲜度。
证据哈希、目标定义和必需检查仍受校验。
票据重验文档保存在会话目录中，不创建提交；正常 build 仍发布并提交规格执行文档。
