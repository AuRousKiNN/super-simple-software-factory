# Codex SDK M0 能力探针

这个目录只验证 `docs/pi-to-codex-design.md` 的 M0，不是生产适配器。它固定使用
`openai-codex==0.155.1` 及该包携带的 runtime，覆盖：

- 全部现有 envelope 的结构化输出；
- 同一 thread 的第二轮与新进程 resume；
- `developer_instructions`；
- 流事件、工具生命周期与用量；
- turn 取消及取消后子进程清理；
- 单个只读子代理、父子 thread 关系和用量归属可见性。

在锁定版本中，父流以 `subAgentActivity.agentThreadId` 报告子 thread 生命周期；
`collabAgentToolCall(wait)` 的 `receiverThreadIds` 可能为空，不能单独用它建立父子关系。

SDK 会原样转发 `output_schema`。探针先将 Pydantic schema 严格化：所有对象补
`additionalProperties: false`，所有属性进入 `required`，并去掉 host 侧默认值；
原始 `model_json_schema()` 会被响应格式接口拒绝，不能在生产适配器中直传。

真实输出包含 thread/turn/item ID、绝对路径和原始事件，必须写到仓库外：

```bash
uv run --with openai-codex==0.155.1 \
  python experiments/codex_sdk_m0/probe.py all \
  --output-dir /tmp/sssf-codex-sdk-m0
```

探针的 `all` 命令会为 `baseline` 和 `resume` 启动两个独立 Python 进程，以免把
“关闭并重开 SDK 客户端”误当成跨进程恢复。默认采用只读 sandbox、拒绝所有新增
审批请求，并使用 `gpt-5.6-terra` 的 low effort 降低验证成本。

确定性测试也必须按仓库规则提权运行：

```bash
uv run --with openai-codex==0.155.1 --with pytest \
  pytest -q experiments/codex_sdk_m0/test_probe.py
```

不要提交探针的原始输出。仓库中的事件样本必须先脱敏，只保留重现事件转换所需的
字段。
