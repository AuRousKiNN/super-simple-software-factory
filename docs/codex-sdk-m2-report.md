# Codex SDK M2 实施报告

状态：M2 代码与确定性验收已完成。核对日期：2026-09-21。

## 已交付

- 权限快照由 Git 行数差异升级为内容级状态：覆盖 tracked、已有 untracked、删除/重命名、
  文件类型、权限位、symlink 目标和 Git index；恢复副本位于仓库外，不使用 `HEAD` 或
  `git reset --hard`，因此可以原样保护 agent 启动前已经存在的 dirty 内容。
- 仓库级 advisory write lock 阻止两个 SSSF 运行同时修改同一工作树。权限核验在每个 Codex
  turn 的成功、失败、超时和取消出口执行，phase 最终再核验一次；越权不进入 JSON/gate
  修复循环。回滚前再次比对当前指纹，若核验期间又有外部写入则停止覆盖该路径并报告并发
  冲突。`data_dir` 不再整体放行，只识别当前 handoff/report 路径。
- 每个 agent 调用建立独立 invocation 目录；原始 SDK notifications、编译后的 prompts、输出
  schema 和 envelope 分开保存，同时保留最新 envelope/prompts 兼容视图。
- runtime 发现并登记 SDK 新建的直接 app-server 子进程，保存 PID、启动标识和命令。
  `Codex.close()` 有界等待；超时后仅对启动标识仍匹配的所属进程执行 TERM/KILL。SIGTERM、
  timeout 和异常路径均进入统一收尾。
- Codex 工具事件按 invocation + thread + turn + item 去重；只有 completed 的事件显式记录
  未知开始时间，未完成工具随 interrupted/failed turn 记录为失败；未知 notification 原样落盘
  并计数。
- 用量采用 schema v2：cached input 不重复加入 input，reasoning 不重复加入 output；缺少 per-turn
  `last` 时用持久化 thread baseline 对累计值求增量。线程累计 token 不再伪装成上下文占用。
- SQLite 使用显式 `schema_meta=2`，新增 `agent_invocations` 和 turn settlement 去重；非当前或
  不完整数据库明确拒绝。费用保存已知小计及 `cost_complete`，未知费用在终端/UI 显示为
  `unknown`，不显示为 `$0`。
- Visualizer server、共享类型和 Vue 组件已切换到 Codex usage/cost/tool 合同，并拒绝旧 DB。

## 确定性验收

- Python 全套：`18 passed`。
- Visualizer：`npm run build` 通过（含 `vue-tsc --noEmit` 与 Vite production build）。
- Visualizer lint：通过；保留一条与本次改动无关的 `models.ts` 既有
  `prefer-array-find` warning。

覆盖的 M2 故障包括：相同行数内容改写、已有 untracked 改写、mode/symlink/index 改写、
失败 turn 越权恢复及 `agent_end`、累计 usage 求差、未知事件、工具完成去重、旧 DB 拒绝、
同一 turn 只结算一次，以及 runtime close 必定执行 owned-process cleanup。

## 尚未执行

真实单 agent smoke 和禁用子代理的完整 SDLC 会调用远端模型并产生数据外发与用量，当前没有
把“执行 M2”扩大解释为该授权，因此未运行。确定性 fake transport、故障注入、Python 测试和
前端构建均不依赖真实模型。获得明确授权后，应在无 Pi 环境的临时目标仓库运行完整 SDLC，
并补验 timeout/SIGTERM 后系统进程表无遗留、真实事件字段与费用仍为 unknown。
