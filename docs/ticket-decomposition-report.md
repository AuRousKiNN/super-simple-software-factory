# Decomposer 首版实现与验证

日期：2026-09-22。

## 已实现

- decomposer roster、提示词、DecomposeOutput，以及只读 sssf_recon 委派支持。
- 独立 `adw-decompose --spec` 与 `adw-plan-decompose` 入口；生成器和直接角色入口使用具体输出类型。
- planner 返回归档 `spec_path`；builder 支持直接请求、完整 spec、选定 ticket 三种互斥输入。
- 工件摘要、原子索引、依赖图、前置验收记录与实现基线检查；修复和 review 保留原 work_item。
- session 绑定、规划 revision 保留、当前输出目录限制，以及输入和 host 绑定文件的只读保护。
- 失败 envelope 持久化；失败、中断、取消时恢复越权修改。信号处理保持工作区锁直到权限恢复完成。
- 新安装分发、已有配置与提示词保留、升级合并指引，以及 README、使用文档和角色导览同步。

## 工件格式策略

按用户补充要求，不对 decomposer 工件做格式校验，也不添加专属格式 gate。
没有 frontmatter 严格 schema、字段白名单、重复 key 检查、固定章节、AC 表格、标识符格式、kind/profile 枚举或文件名与 ID 一致性检查。

Markdown 正文作为不透明内容保留。索引读取必要元数据；字段以外的正文与扩展信息不决定工件是否通过。必要输入无法读取时，消费输入的 code 阶段报告错误，不发起工件格式纠错。
路径权限、来源摘要、身份归属及依赖关系属于保留的运行约束。所有角色继续使用既有的类型化 JSON envelope 机制。

## 验证

所有测试均提权执行：

```text
.venv/bin/python -m pytest -q
65 passed in 5.72s
```

覆盖自由正文与扩展元数据、依赖图与索引变化、目标恢复与多轮反馈、依赖证据和基线、各退出路径的权限恢复、host 验收发布、安装保留定制和工作流生成。`git diff --check` 通过。

最新真实 smoke 使用临时合成 Git 仓库，执行：

```text
spec → decomposer → sssf_recon → code(index)
选定 TICKET-GREETING → 独立 builder session → host 精确字节检查
```

decomposer 单次业务 turn 完成，没有格式 gate 或格式纠错；一个 sssf_recon 子代理正常完成。两个 ADW 均由 run.finish 记录成功，主程序确认 greeting.txt 的内容精确符合要求。
复现入口为 `experiments/ticket_decomposition/probe.py --output-dir <新目录>`；本次摘要和日志保存在 `tmp/ticket-smoke-no-format-gate-20260922/`。

## 使用边界

build 入口报告实施结果，不自动签发依赖验收记录。具体 ADW 负责实际检查、review、强制人工验证及整体集成验收。
核心前置证据采用精确 Git 基线匹配；基线或环境变化后的适用性由 ADW 重新确认。真实 smoke 验证单张无 blocker ticket；多 blocker、证据不足和定义变化的路径由确定性测试覆盖。
