# Codex SDK M3 实施报告

状态：M3 代码与确定性验收已完成；真实合成 smoke 已验证 child 生命周期和只读无残留；逐
child 的 custom role 加载证明与真实完整 SDLC 待明确授权。
核对日期：2026-09-21。

## 已交付

- starter roster 只对 planner、scout 启用原生子代理；其他角色继承关闭状态。父级配置通过
  `agents.enabled` 和 `agents.max_concurrent_threads_per_session` 下发，SSSF schema 继续把并发
  上限限制在 1–6。
- 新增项目级 `sssf_recon` 自定义角色：`sandbox_mode="read-only"`，角色内
  `agents.enabled=false`，因此 child 没有再次创建 child 的 multi-agent 工具；安装时还把
  SSSF orchestrator skill 的实际绝对路径写入 `skills.config` 并禁用。角色不固定模型或
  reasoning effort，默认继承父级。
- 配置预检只允许 planner/scout 启用子代理，并验证角色文件位于 `.codex/agents/`、角色名
  匹配、sandbox 为 read-only、递归工具关闭、SSSF skill 已禁用。错误发生在业务 turn 前。
- planner/scout 的 developer instructions 现在要求：只拆分独立只读调查、给出显式且有界的
  task、最多使用配置的并发数、不轮询/打断、等待所有 child 终结后由 parent 汇总。
- runtime 记录 parent/child thread 与 parent turn 关系、声明角色、runtime `agentPath`、任务、
  模型/effort 覆盖、状态、结果、错误和可见 usage。`agentPath` 是父级分配的层级标签，不冒充
  custom role 名。事件流新增 `subagent_start/subagent_end/subagent_result/subagent_log`，
  `agent_end` 保存本次 phase 的 child 汇总。
- 运行时同时执行第二层约束检查：禁用状态下生成 child、超过并发上限，或
  parent terminal 时仍有活跃 child，都会把 parent turn 判为 `subagent_policy` 失败。最后一种
  情况会关闭该 SDK client 及其所属 app-server 进程，防止 child 遗留到 phase 之外。
- child usage 只在出现 child thread 自己的 usage notification 时标为 `separate`；否则为
  `unknown`。它不会叠加到 parent usage，避免 runtime 未声明包含关系时重复计费。
- installer 和 `make_config.py` 会生成项目级角色文件；Visualizer 类型、颜色和展开事件列表已
  接受新的 child lifecycle 事件。

这些配置字段、自定义 agent 文件、只读 sandbox 与 role-local config layer 均来自
[OpenAI 官方子代理文档](https://learn.chatgpt.com/docs/agent-configuration/subagents)。

## 确定性验收

- Python 全套：`25 passed`。
- 新增覆盖：只允许 planner/scout、角色文件硬约束、父级 runtime 配置、父子关系、结果与独立
  usage 记录、父用量不重复计数、禁用状态异常生成 child、超过并发上限、未收尾 child 或事件流
  异常时强制失败/关闭、严格 runtime 版本识别，以及
  `plan → build → test → review → document` 完整确定性链路。
- Visualizer：`npm run build` 通过（`vue-tsc --noEmit` + Vite production build）。
- Visualizer lint：通过；保留一条与 M3 无关的 `models.ts` 既有
  `prefer-array-find` warning。
- M3 修改文件的聚焦 Ruff `F,E9` 检查通过。仓库全量 Ruff 仍会报告迁移前保留代码与既有格式
  问题；M4 删除旧集成时统一收口。

## 已执行的真实合成 smoke

- 在一次性临时目录中只创建 `alpha-marker`、`beta-marker` 两个合成文件，不发送本仓库业务源码。
- 锁定的 SDK/runtime 按 `sssf_recon` 声明策略成功创建两个 child，parent 等待完成并汇总两个
  文件 marker。
- 两个 child 均出现 started/completed 生命周期，parent 正常 completed，未触发强制 cleanup；
  synthetic workspace 的内容摘要前后完全一致，验证本次 read-only 路径未留下修改。
- 真实事件确认 `agentPath` 是 `/root/alpha_probe`、`/root/beta_probe` 这类层级标签，而非 custom
  role 名；实现已据此分开保存 `agent_path` 与声明 role。
- 真实流仍只有 parent thread usage，`child_usage_attribution=unknown`，实现没有把 child 用量重复
  加入 parent。脱敏前原始记录与 summary 保存在仓库外 `/tmp/sssf-codex-sdk-m3-smoke/`。
- smoke 还发现 runtime version 字段可能为 `0.155.1 (platform...)`；预检现提取并精确比较开头
  semver，回归测试覆盖该格式。

这次成功 smoke 的父提示本身包含了预期角色标记，因此它不能独立证明两个 child 都加载了
custom role；事件中的 `agentPath` 也不提供该信息。更严格的探针已改为让 parent 在不知道预期值
的情况下，分别原样回传两个 child 的首 token，再核对二者均为 `SSSF_RECON_OK`。该远端调用在
执行前因缺少明确数据外发授权被权限审查拒绝，没有绕过执行。

## 尚未执行

还需要两项真实验证：严格的逐 child 角色标记 probe；完整
`plan → build → test → review → document`。前者只外发两个合成 marker、角色配置和验证提示，
后者会外发工厂模板与合成目标仓库并产生多次模型调用用量。两项都因缺少明确数据外发授权而
没有继续执行。获得授权后，可分别运行 `experiments/codex_sdk_m3/probe.py` 和
`experiments/codex_sdk_m3/full_sdlc_probe.py`；原始 SDK notification 会保存到仓库外。
