# Codex SDK M0 验证报告

状态：M0 已完成。验证日期：2026-09-21。

本报告验证 `pi-to-codex-design.md` 的 M0，不包含生产适配器改造。M1 可以基于本报告
启动；M3 必须继续保持子代理默认关闭，直到本文列出的硬约束和用量归属缺口解决。

## 版本锁定

| 对象 | 锁定值 | 证据 |
|---|---|---|
| Python SDK | `openai-codex==0.155.1` | `importlib.metadata.version()` 与锁文件一致 |
| SDK 携带 runtime | `0.155.1` | 初始化元数据为 `Codex Desktop/0.155.1` |
| 验证 Python | `3.14.5` | `uv run` 的隔离解释器；SDK 要求 Python 3.10+ |
| 验证平台 | macOS 26.4.1 arm64 | runtime 初始化元数据 |
| 验证模型 | `gpt-5.6-terra` | `models()` 返回可用，multi-agent v2 |
| 验证 effort | `low` | 模型能力列表包含 `low/medium/high/xhigh/max/ultra` |
| 观测上下文窗口 | 258,400 tokens | `thread/tokenUsage/updated.modelContextWindow` |

M1 应精确锁定 `openai-codex==0.155.1` 并默认使用包内 runtime。宿主机 PATH 上的
`codex-cli 0.154.0` 只用于诊断，没有参与本次 SDK 验证，也不应通过 `CODEX_PATH`
无条件替换锁定 runtime。官方文档确认发布的 Python SDK 自带固定 runtime，且只有
显式设置 `CodexConfig(codex_bin=...)` 才使用其他二进制：[Codex SDK](https://learn.chatgpt.com/docs/codex-sdk)。

## 能力矩阵

| 能力 | 结果 | M1/M2/M3 结论 |
|---|---|---|
| 结构化输出 | 通过 | 8 种现有 envelope 均完成并通过 Pydantic 回验 |
| 原始 Pydantic schema 直传 | 不通过 | 服务端 400：每个 object 必须有 `additionalProperties: false`；M1 必须保留 schema 转换层 |
| 嵌套 `$defs` | 通过 | `ScoutFinding`、`ReviewFinding` 均产生并回验嵌套对象 |
| 默认字段与 required | 通过转换后 | 去掉 `default`，将所有 properties 设为 required；宿主默认值不能直接当 wire 约束 |
| 同 thread 第二轮 | 通过 | 第二轮保留首轮 continuity marker，thread ID 不变 |
| 跨进程 resume | 通过 | 独立 Python 进程恢复相同 thread ID，并保留早期 marker |
| 角色 developer instructions | 通过 | 每次结果的 `summary` 都服从仅存在于 developer instructions 的 marker |
| 恢复时角色指令 | 有条件通过 | 本次在 `thread_resume` 时重新传入相同指令；M1 必须这样做，不能依赖隐式持久化 |
| 流事件 | 通过 | 公共 `TurnHandle.stream()` 提供 turn、item、delta、usage 和完成事件 |
| final 提取 | 通过 | `agentMessage.phase=final_answer` 可区别早期 commentary 消息 |
| 工具生命周期 | 通过 | `commandExecution` 有 started/completed、item ID、状态、输出和内部 process ID |
| 用量 | 通过 | 同时提供 `last`、thread 累计 `total`、cached input、reasoning output 与上下文窗口 |
| 费用 | 未覆盖 | SDK/runtime 未报告金额；M1/M2 必须使用 `cost=null, cost_kind=unknown` |
| turn 取消 | 通过 | `interrupt()` 后最终状态为 `interrupted` |
| 取消后命令清理 | 通过本机 smoke | 中断 30 秒命令后 2 秒内未发现匹配的残留进程 |
| 单子代理 | 通过 | 限制并发为 1，出现同一 child thread 的 started/completed activity，父级等待并回传结果 |
| 子代理事件关联 | 部分通过 | 关系来自 `subAgentActivity.agentThreadId`；`collabAgentToolCall(wait).receiverThreadIds` 实测为空 |
| 子代理用量归属 | 不通过 | 父流只有父 thread ID 的累计 usage，无法证明 child 是否已包含，也无法独立结算 |
| 禁止子代理递归 | 未覆盖 | 公开全局配置没有最大深度/禁递归字段；本次只通过任务指令要求 child 不再创建 agent |

app-server 的 thread/turn/resume/interrupt 语义与本次行为一致，见
[App Server 文档](https://learn.chatgpt.com/docs/app-server)。官方子代理文档也确认子代理继承
父级 sandbox，并公开并发上限但未提供递归深度开关，见
[Subagents 文档](https://learn.chatgpt.com/docs/agent-configuration/subagents)。

## 事件与统计结论

脱敏录制样本位于：

- `experiments/codex_sdk_m0/samples/stream.events.jsonl`
- `experiments/codex_sdk_m0/samples/cancel.events.jsonl`
- `experiments/codex_sdk_m0/samples/subagent.events.jsonl`

这些文件是 SDK notification 的 JSON 序列化，不是 app-server stdout 的逐字节抓包。完整原始
事件只保存在本次临时验证目录，没有提交真实 thread/turn/item ID 或本机路径。

用量字段验证了设计中的口径：`inputTokens` 已包含 `cachedInputTokens`，两者不能相加；
`reasoningOutputTokens` 是 output 的细分，也不能再次加入 total。恢复后的 `total` 是 thread
累计值，单次 invocation 应从已持久化基线计算增量，而 `last` 只能描述最近一次 usage 更新。

## SDK 行为约束

1. `Thread.run()` 在失败 turn 上抛出 `RuntimeError`。要保存失败前事件和用量，生产适配器
   必须使用 `turn()` + `stream()` 自行收集，不能只包一层 `run()`。
2. SDK 将未知或无法解析的通知保留为 `UnknownNotification.params`，可满足“原样留档后计数”；
   但高层接口不提供 wire bytes，记录必须标为 SDK notification。
3. 高层 `Thread` 只公开 thread ID，没有直接暴露协议返回的 `instructionSources`。M1 若必须
   记录它，应先确认稳定公开入口；不能依赖 `_client` 等私有属性。
4. 高层 SDK 没有 app-server 子进程的 spawn/exit 回调、公开 PID 或 stderr 流。正常
   `Codex.close()` 可以关闭本次客户端；M2 仍需为超时、崩溃和宿主终止补充进程身份与兜底清理。
5. `subAgentActivity` 的 started/completed 分别同时出现在 `item/started` 与 `item/completed`，
   持久化时必须按 `thread + turn + item + kind` 去重，不能把四条通知计成四个子代理。

## 未覆盖能力与推进条件

- M1 可推进：结构化输出、会话、恢复、角色指令、final 提取和基础 usage 已验证。
- M2 开始前要决定如何获得 app-server PID/启动标识/stderr，或证明 `close()` 加宿主进程组
  管理足以满足异常清理；本次只验证了正常 SDK 中断路径。
- M3 保持禁用，直到能硬性禁止 child 再创建 subagent、验证自定义角色权限确实为只读、
  明确 child usage 是否包含在 parent usage 并提供可靠归属、验证父取消能收尾所有 children。
- 模型拒绝、认证失败、断线、长 stderr、乱序/重复/未知事件属于 M1/M2 的 fake transport
  与故障注入范围，不用真实模型稳定性替代确定性测试。

## 复现

探针与精确依赖锁位于 `experiments/codex_sdk_m0/`。真实输出应写到仓库外：

```bash
uv run --with openai-codex==0.155.1 \
  python experiments/codex_sdk_m0/probe.py all \
  --output-dir /tmp/sssf-codex-sdk-m0
```

本次确定性测试结果：`6 passed`。真实 smoke 的最终断言全部通过；首次 raw Pydantic schema
请求按预期记录为失败，加入严格 schema 转换后重跑通过。
