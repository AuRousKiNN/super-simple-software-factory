# SSSF Ticket 拆解与 Builder 双输入最终设计

状态：首版实现已完成；确定性测试与真实 spec → decomposer/recon → ticket builder smoke 已通过。
实现与验收记录见 [ticket-decomposition-report.md](ticket-decomposition-report.md)。
日期：2026-09-22。

## 1. 目标

新增 `decomposer` 角色，将 planner 输出的完整 spec 拆成一组 tickets。每张 ticket 是有明确范围和验收标准的交付单元，使用 `blocked_by` 声明真实前置依赖。

builder 支持两种实施目标：

- 完整 spec：实施整个需求。
- 指定 ticket：实施当前交付单元，并遵守根 spec 的适用公共契约。

拆解作为可选阶段接入，保留 planner → builder 的直接路径。项目统一使用 ticket、ticket_id 和 blocked_by；ID 用于稳定标识，依赖关系决定执行资格。

首版交付角色、工件、结构化交接、双输入、校验及独立调用入口。ticket 选择、执行方式、暂停和提交策略由具体 ADW 决定。

## 2. 角色与所有权

| 角色/组件 | 职责 | 可写产物 |
|---|---|---|
| planner | 完整需求、公共契约、范围、全局验收标准和风险 | 根 spec 及其交接副本 |
| decomposer | ticket 边界、真实依赖、验收责任、需求追踪与集成义务 | 当前 ticket 集合的规划工件 |
| recon subagent | 受委派的仓库调查，核实交付边界、依赖和验证入口 | 向父代理返回证据摘要 |
| builder | 实施完整 spec 或指定 ticket，响应检查和评审反馈 | 当前范围内的代码与实施报告 |
| reviewer | 对照目标和变更判断需求是否满足；按 ADW 指定范围审查拆解或集成结果 | 评审报告 |
| Python 工作流 | 工件解析、输入绑定、依赖检查、检查命令、证据交接、执行选择与最终验收 | 派生索引、运行记录和验收记录 |

planner 聚焦完整需求，根 spec 应足以直接实施。ticket 边界、AC 分配和 ticket 依赖统一由 decomposer 维护。

decomposer 通过仓库调查核实影响拆分的事实。规格缺少公共语义时，将具体问题和影响交接 planner。实施代码、运行检查、Git 提交和执行状态分别由其所属角色或 code 阶段处理。

builder 以绑定的实施目标处理每轮反馈；范围或依赖问题交接 decomposer，根规格问题交接 planner。

### 2.1 Decomposer 使用 recon

decomposer 与 planner、scout 一样，可以委派 `sssf_recon` 子代理。starter roster 为 decomposer 启用现有 recon 配置：

```yaml
subagents:
  enabled: true
  max_concurrent: 6
  role: sssf_recon
  config_file: .codex/agents/sssf_recon.toml
```

decomposer 负责拆分判断和规划写入，recon 承担边界清楚、可独立进行的只读调查，例如：

- 核实某个交付行为贯穿的真实调用链。
- 确认拟定 blocker 提供的能力是否已存在。
- 查找公共契约、共享修改点及已有验证证据。

每次委派提供自包含任务和明确的证据需求。recon 返回文件、符号和事实依据；decomposer 等待所创建的全部子代理结束，处理失败并汇总证据后，再完成拆解和交接。

recon 沿用现有只读 sandbox 和叶子代理配置，SSSF orchestrator skill 与子代理能力保持禁用。父子线程、状态、错误、用量和取消清理沿用现有运行时机制。

模型和 thinking 使用 roster 的现有配置机制。decomposer 的 system prompt 接入 `{{subagent_instructions}}`；运行时允许启用 recon 的父角色集合扩展为 planner、scout、decomposer。

## 3. Ticket 划分规则

### 3.1 普通需求

每张 ticket 交付一个范围窄但完整的行为，贯穿该行为实际需要的层，并包含相应验证责任。

满足前置条件后，一张 ticket 应当：

1. 能被新的 agent 上下文独立理解和实施。
2. 有可观察的交付结果和明确的验收终点。
3. 能在声明的环境与前提下独立演示或验证。
4. 保留必要契约，范围适合一个新上下文完成。
5. 清楚区分已有能力、当前增量和其他 ticket 的责任。

一个 spec 可以只产生一张 ticket。进一步拆分依据独立交付价值、真实依赖、风险隔离或上下文负担。

### 3.2 依赖

`blocked_by` 记录必须先完成的 ticket ID：

- 空列表表示没有 ticket 前置依赖。
- 所有依赖都指向当前集合中的有效 ticket，整体形成有向无环图。
- ticket 可以依赖任意 ID；展示顺序可按阅读需要调整。
- 共享文件修改冲突记录为协调事项。
- 并行执行资格由 ADW 结合依赖、资源和工作区隔离判断。
- 外部系统、人工决策和环境要求记录为前提或阻塞事项。

例如：

```text
TICKET-QUERY   blocked_by: []
TICKET-AUDIT   blocked_by: []
TICKET-EXPORT  blocked_by: [TICKET-QUERY]
```

QUERY 与 AUDIT 可分别成为执行候选；EXPORT 在 QUERY 的前置交付满足后成为候选。

### 3.3 大范围重构

公共类型替换、接口迁移等大范围机械变更采用 expand–migrate–contract：

1. 引入兼容的新形式，保留旧形式。
2. 按影响范围拆分调用方迁移，每批依赖 expand。
3. 全部必要迁移完成后，移除旧形式。

优先使每批独立保持构建和检查通过。需要共享集成基线的批次，明确局部证据限制和最终集成责任，由专用 ADW 安排。

前置重构说明它解决的真实阻塞。普通功能的全局验证写入集成义务；具有独立集成交付工作的重构可以建立 integration ticket。

## 4. 规划工件

planner 在固定路径维护当前 spec。拆解工件与根 spec 相邻，一张 ticket 一个 Markdown 文件：

```text
specs/
├── <adw_id>_<slug>.md
└── <adw_id>_<slug>.tickets/
    ├── ticket-set.md
    ├── tickets/
    │   ├── TICKET-QUERY.md
    │   ├── TICKET-AUDIT.md
    │   └── TICKET-EXPORT.md
    └── index.json
```

| 工件 | 内容与维护者 |
|---|---|
| 根 spec | planner 维护公共语义和整体完成标准 |
| ticket-set.md | decomposer 维护集合成员、来源、集成义务、协调事项、拆分理由和修订影响 |
| ticket Markdown | decomposer 维护当前 ticket 的身份、依赖、范围和验收责任 |
| index.json | Python 从已验证规划工件生成机器索引 |

公共契约按根 spec 的编号引用，总览从规划工件生成。

### 4.1 路径与事实来源

结构化路径统一使用仓库根相对 POSIX 路径，解析结果必须位于仓库内并符合预期文件类型。

集合成员以 ticket-set.md 的 frontmatter 为准；每张 ticket 的依赖以该文件的 frontmatter 为准，正文解释依赖理由。

根 spec 使用稳定的 REQ、公共契约和 AC 标识。ticket 引用这些标识，定义自己负责的可观察结果；公共语义统一由根 spec 维护。

### 4.2 ticket-set.md 示例

```markdown
---
schema_version: 1
revision: 1
source_spec: specs/abc_accounts.md
tickets:
  - specs/abc_accounts.tickets/tickets/TICKET-QUERY.md
  - specs/abc_accounts.tickets/tickets/TICKET-EXPORT.md
---

# 账户能力 Ticket 集合

## 整体集成义务
全部交付集成后需满足的根规格 AC、跨 ticket 行为，以及必需的检查和人工验证。

## 协调事项
共享出口、注册或装配的修改冲突及协调要求。

## 拆分依据与修订影响
交付边界的理由、证据缺口，以及本次修订影响的 tickets 和集成证据。
```

成员列表应非空、唯一且完整，明确本次拆解的全部 tickets。

### 4.3 单张 ticket 示例

```markdown
---
schema_version: 1
id: TICKET-EXPORT
revision: 1
kind: behavior
profile: standard
blocked_by: [TICKET-QUERY]
requirements: [REQ-03]
---

# 导出账户查询结果

## 交付行为与范围
用户能将符合筛选条件的账户导出；说明已有查询能力和本次新增行为。

## 公共契约与前提
引用根 spec 的适用合同，说明需要前置 ticket 提供的能力及环境前提。

## 验收标准
| AC | 可观察结果 | 证据责任 |
|---|---|---|
| AC-EXPORT-01 | 导出内容与筛选条件一致 | 验证真实查询到导出的链路 |

## 验证与风险
必要的回归保护、可复用证据、需真实执行的机制和残余风险。
需要时区分 Required Manual Validation 与 Optional Smoke。
```

`kind` 取 behavior、refactor 或 integration，描述交付性质；`profile` 取 standard 或 critical，描述风险后果。

Standard 使用最小充分证据。Critical 针对具体高影响后果补充稳定 CASE、现实触发、稳定终态和需守住的副作用边界。

文件路径、内部符号、函数签名和实现片段由 builder 根据当前代码确定。规格保留用户明确约束的公共 API；原型中的关键决策片段可以附来源引用。

## 5. 类型与交接

### 5.1 Planner 与 decomposer 输出

`PlanOutput` 新增 `spec_path`，成功输出明确指向当前权威 spec，现有交接文件作为副本。

新增 `DecomposeOutput(EnvelopeBase)`：

| 字段 | 含义 |
|---|---|
| status | success / fail |
| summary | 拆解结果或具体阻塞 |
| artifacts | 已实际保存的集合和 ticket 文件 |
| ticket_set_path | ticket-set.md 路径；失败且尚未写出时可为空 |
| outcome | ready / needs_spec_revision / needs_decision / artifact_error |
| commit_message | 概括本次拆解工件，遵循目标仓库提交规范 |
| notes_for_next_agent | 实施、修订或决策交接说明 |

`status=success` 与 `outcome=ready` 对应，且工件满足成功输出合同。其余 outcome 使用 fail，保存已知内容和具体阻塞。

DecomposeOutput 通过 ticket_set_path 引用完整定义。Python 在 agent 阶段结束后生成 index.json，并记录派生产物。

`ready` 表示拆解定义已具备交接条件。执行资格和验收结果由工作流分别判断。

### 5.2 Builder 双输入

在 `AgentCall` 新增可选 `work_item`，使用带 kind 判别字段的具体类型联合：

| 类型 | 主要内容 |
|---|---|
| SpecWorkItem | kind=spec；根 spec 的路径与内容摘要 |
| TicketWorkItem | kind=ticket；根 spec、集合和当前 ticket 的引用、定义摘要，以及前置证据引用 |

工件引用使用具体 `ArtifactRef` 类型，至少包含 path 与 sha256。Python 负责解析来源、计算摘要和装配输入。

user prompt 增加 `{{work_item}}`：

- spec 模式实施完整根 spec。
- ticket 模式实施明确选中的 ticket，读取适用公共契约与前置交付信息。
- 每轮修复继续传入相同 work_item。
- 原有 previous 传递测试、review 等最近一轮反馈。
- 根规格与 ticket 发生矛盾时，保存具体问题并交接对应角色。
- 其他角色可以通过 work_item 获取相同的审查对象。

保留直接 prompt 实施入口。未传 work_item 时按直接请求处理；来自 planner 的规格调用转换为 SpecWorkItem。

`BuildOutput` 保留实施报告职责。Git 变更由 changes 模块捕获，验收由工作流决定。

### 5.3 对话与目标绑定

每个 ticket 使用独立 ADW session，由入口默认分配新 adw_id。同一 ticket 的修复复用原 session/thread，沿用当前 adw_id + role 的线程映射。

运行时在 host 管理的 session 文件中绑定 work_item 身份和定义摘要。恢复前核对绑定；目标或定义变化时，返回明确原因并要求使用匹配的输入或新 session。

## 6. 验证、权限与索引

### 6.1 输入绑定与运行约束

按 2026-09-22 的补充决定，不对 decomposer 生成的工件做格式校验：不增加专属格式 gate，不检查 frontmatter 严格 schema、字段白名单、重复 YAML key、固定章节、正文结构、AC 表格、ID 格式或 kind/profile 枚举。

Python 在派发前绑定根 spec 与输出目录并记录摘要；返回后核对来源未变。索引阶段仅消费需要的元数据，正文视为不透明文本。无法读取的必要元数据或实际文件会使消费该输入的 code 阶段失败，不触发 agent 工件格式纠错。

路径权限、来源绑定、ticket 身份唯一、集合归属与依赖图无环仍作为运行约束处理。需求覆盖、AC 充分性、真实依赖和拆分粒度由 decomposer 自查，ADW 可增加 reviewer 审查。

### 6.2 派生索引

code 阶段读取规划元数据后原子生成 index.json，包含：

- 索引 schema 版本；
- 根 spec 的引用和内容摘要；
- 集合文件及成员的引用和内容摘要；
- frontmatter 中的 ID、revision、kind、profile、blocked_by 和需求引用；
- 整个定义集合的确定性摘要。

加载时核对 Markdown 与索引一致。源工件发生变化时重新验证并生成索引；索引排序用于稳定展示。

### 6.3 写入边界

decomposer 的静态 writes 开放 ticket 规划区域，每次调用进一步限定到指定输出目录。报告和 context_handoff 沿用现有运行时写例外，recon 保持只读调查权限。

builder 将当前根 spec、完整 ticket 集合和索引作为只读输入。permissions 沿用快照和恢复机制，动态输入保护优先于宽泛 writes。

权限验证覆盖成功、失败、中断和取消。越权写入按现有恢复机制处理，并记录明确的阶段失败。

## 7. 执行资格与验收

规划工件描述交付定义和验收义务，执行与验收状态由 ADW 记录。

当前 ticket 可执行须满足：

- 当前定义通过校验，输入及环境前提可用。
- 每个 blocker 都有可追溯、适用于当前定义的完成/验收证据。
- 所需前置实现已进入当前代码基线。
- 相关规格冲突和修订影响已处理。

前置证据定位 ticket 身份和定义摘要、产生证据的运行、检查/评审工件及实现基线。工作流核对实现已进入当前基线，并确认检查和评审证据的适用性。相关代码、测试、配置或环境变化时重新确认；依据不足时记录阻塞。

具体 ADW 提供前置结果和验收策略，核心模块校验输入及证据引用。独立 ticket 入口在必要证据齐备后派发 builder。

检查由 kind=code 阶段运行，证据应证明要求的检查和目标测试实际执行。强制人工验证需要有完成记录。修复沿用现有有界循环。

单张 ticket 的实施、检查、评审分别记录。整体完成要求所有必要交付在同一基线上成立，并满足 ticket-set.md 的集成义务及根 spec 的完成标准。集成验收由 ADW 根据实际完成状态安排。

最终结论由 `run.finish(accepted=..., reason=...)` 记录。build 入口报告实施调用结果；完成验收合同的 ADW 产出可用于后续依赖的验收记录。

## 8. 工作流接入与失败路径

新增：

- `adw-decompose`：明确 spec → decomposer → code 校验/生成索引。
- `adw-plan-decompose`：request → planner → decomposer → code 校验/生成索引。

builder 入口提供三种互斥模式：

| 入口 | 输入 |
|---|---|
| 原有位置参数 | 直接请求 |
| `--spec <spec.md>` | 完整 spec |
| `--ticket <ticket.md>` | 指定 ticket |

ticket 模式通过 `--ticket-set <ticket-set.md>` 绑定集合，通过 `--dependency-evidence <json>` 提供必要的 host 验收记录引用。有 blocker 时证据必须齐备。

入口校验 ticket 的集合归属，并解析根 spec、索引和证据。组合 ADW 可以根据 DecomposeOutput 和明确的 ticket 选择，直接装配等价的强类型输入。

现有 plan-build 路径构造 SpecWorkItem。builder 修复及后续 review 始终保留原 work_item，并单独传递最新反馈。ADW 从满足前置条件的 tickets 中显式选择当前实施目标。

| 情况 | 处理 |
|---|---|
| 输出 envelope JSON 失败 | 同一角色线程内有界纠错 |
| 公共需求或合同缺失 | 保存 fail 输出，交接 planner |
| 产品决策待确定 | 保存 fail 输出，交接上层决策 |
| ticket 依赖、归属或范围错误 | 交接 decomposer |
| 前置证据、基线或环境不足 | 派发前记录阻塞 |
| 测试或 review 失败 | 相同 work_item 下进入有界 builder 修复 |
| 权限、认证、超时或运行时失败 | 沿用现有错误分类与终止处理 |

fail envelope 持久化后终止当前阶段。上层根据保存的结构化原因发起后续调用。

新 ADW 预先验证 required roster，在 code 校验和必要验收结束后记录完成。使用 recon 的 decomposer 阶段，在全部子代理结束并处理其结果后通过阶段验收。

## 9. 修订与来源有效性

根 spec 由 planner 在固定路径维护，拆解通过 source_spec 引用当前来源，并由内容摘要绑定具体定义。

拆解集合与各 ticket 在固定的 `<spec>.tickets/` 目录维护。修订直接更新对应文件，保留稳定 ticket ID。根 spec、ticket-set 和单张 ticket 均在文档内记录整数 revision，初始为 1；每次修订加 1，包括文案和展示顺序调整。未修改的文档保持当前 revision。

发布阶段刷新派生索引，并将索引引用写入当前 session 的 decomposition-published.json。此后修订使用新 session，继续更新原路径文件。已绑定的实现 session 通过内容哈希识别定义变化；新目标重新绑定后执行。

修订说明列出受影响的 tickets 和证据。工作流复用历史证据前确认当前适用性，依据不足时记录阻塞或重新验证。

## 10. 实施落点与发布

以下路径相对 .agents/skills/sssf/：

| 位置 | 改动 |
|---|---|
| templates/sssf.config.yaml | 新增 decomposer 并启用 recon；调整角色职责和权限配置 |
| templates/prompt_engineering/decomposer/ | 新增提示词、subagent_instructions 和 DecomposeOutput 示例 |
| templates/prompt_engineering/planner/ | 聚焦完整 spec；显式输出稳定的 spec_path |
| templates/prompt_engineering/builder/ | spec/ticket 双输入和固定目标的修复规则 |
| templates/prompt_engineering/reviewer/ | 读取明确审查对象和原 work_item |
| templates/adws/adw_modules/data_types.py | 输出、ArtifactRef 与 work_item 类型 |
| templates/adws/adw_modules/tickets.py（新增） | 解析、依赖校验、索引、输入装配及前置证据校验 |
| templates/adws/adw_modules/agents.py | 渲染 work_item；将 decomposer 加入 _SUBAGENT_PARENT_ROLES，更新校验和提示 |
| templates/adws/adw_modules/permissions.py | 本次输出范围和输入只读保护 |
| templates/adws/adw_modules/runner.py | builder 目标绑定与恢复检查 |
| templates/adws/adw-*.py | 新增拆解入口；接入双输入和修复目标 |
| scripts/make_adw.py | 新增输出类型映射，装配明确的 ticket 目标 |
| scripts/install.py 及升级说明 | 分发新文件，提供配置和提示词合并指引 |
| SKILL.md、cookbooks、references、README 和导览 | 同步角色、ticket 契约和 planner/scout/decomposer 的 recon 权限 |

生成器和直接角色调用入口使用内置角色对应的具体输出类型。生成 decomposer → builder 时，明确 ticket 选择并装配 work_item。

类型、提示词报告示例与 output_type 调用点同步更新。沿用当前运行时、配置和 SQLite schema；工件 schema_version=1 独立管理。新增审计信息使用现有事件及 host 管理的 session 文件。

安装器保留用户配置、提示词和 starter ADW。升级提供角色新增及调用合同变更的合并指引，完成合并后校验新合同。

## 11. 实施验收

验证覆盖以下行为：

1. 多张无前置依赖的 tickets 均可成为候选；任意 ID 的合法依赖图通过校验。
2. 重复 ID、未知 blocker、自依赖、环、缺失工件和路径逃逸产生明确错误。
3. 源工件变化后，索引和绑定输入要求重新验证。
4. spec/ticket 输入正确渲染，多轮修复持续保留原 work_item 和当前 ticket 范围。
5. 依赖证据和实现基线齐备后派发 builder；证据不足时返回具体阻塞。
6. decomposer 仅写当前规划区域，builder 输入保持只读；异常路径恢复越权改动。
7. 不同 ticket 使用独立 session，同一 ticket 修复复用原线程，恢复时核对目标。
8. fail 输出保存真实阻塞，工件不触发格式纠错，envelope JSON 错误与运行时错误按各自机制处理。
9. decomposer 可启用 sssf_recon，父角色配置校验接受 planner/scout/decomposer。
10. recon 使用只读叶子代理权限；并发上限、结束等待、失败汇总、取消清理与追踪保持有效。
11. 新安装包含角色、提示词和入口；已有安装保留定制并提供合同合并指引。
12. 生成工作流使用正确类型和 run.finish，分别记录 ticket 与整体验收结论。

使用确定性模拟运行时验证合同及失败路径；真实 smoke 验证 spec → decomposition、decomposer 委派 recon 和单 ticket 的端到端交接。所有测试按仓库要求提权执行。

## 12. 参考

- ca-core 的 break-into-steps：完整根规格、真实依赖、责任归属、适度验证、公共语义唯一来源及独立集成义务。
- skills 的 engineering/to-tickets：纵向交付、单上下文粒度、一张 ticket 一个文件、blocking edges 及大范围重构的 expand–contract。

参考源：

- /Users/aurous/Documents/GitHub/ca-core/.agents/skills/break-into-steps/SKILL.md
- /Users/aurous/Documents/GitHub/skills/skills/engineering/to-tickets/SKILL.md
