# 规格与执行工件实施验收

日期：2026-09-24。对应 [设计合同](spec-execution-artifacts-design.md)。

## 已实现

- 启动 Agent 显式选择 `--spec-dir` 或 `--spec`。宿主固定规格路径，校验目录冲突、越界、符号链接、会话绑定和 revision；有效的阻塞规划也有最小入口，不宣称已有执行观察。
- 新增 `spec_artifacts.py`，统一维护规格 README、不可覆盖的执行报告、总索引、事实元数据、发布日志及恢复。独立记录分配不同 execution ID，JSON/gate 重试保持当前 ID。
- `DocumentRequest` / `DocumentContext` 显式承载目标、变化、检查、审查回执、历史与发布绑定。`DocumentDraftOutput` 只声明两份暂存正文；宿主发布后返回正式 `DocumentOutput`。
- 完整 SDLC、可控阻塞、复核及生成工作流接入共同记录流程；独立文档工作流被移除。空 diff 不回退到上一提交，也不阻止补证记录。
- planner、decomposer 和 documenter 采用当前目标范围；正式现状、历史、索引和宿主回执不可由 Agent 修改。包括 ignored 文件的越权写入，在失败或取消后仍会恢复。
- `run.finish()` 保存真实结果后重新取锁同步事实。README 哈希冲突、未知完成状态或同步失败会暴露为交付失败，保留回放依据。完整规格验收回执按 execution ID 保存；ticket 回执只更新该 ticket 范围。
- 文档与事实提交使用明确路径，快照排除受管执行文档并保留配置、测试等实现输入。当前验收刷新后会清除不再成立的动态失效提示。
- 安装迁移说明、启动约定、提示词、生成器、操作参考及交互式项目指南均已同步。

主要入口：

- [宿主工件模块](../.agents/skills/sssf/templates/adws/adw_modules/spec_artifacts.py)
- [操作与恢复合同](../.agents/skills/sssf/references/spec-artifacts.md)
- [确定性恢复命令](../.agents/skills/sssf/templates/adws/spec_artifacts_cli.py)
- [发布和失败模式测试](../tests/test_spec_artifacts.py)

## 确定性验证

命令：`.venv/bin/python -m pytest -q`。

最终结果：**138 passed in 21.55s**。覆盖原有运行时、安装、票据与审查分流，以及新增的目录绑定、双文稿结构、空差异补证、不可变历史、证据漂移、README 并发冲突、发布中断恢复、取消后的权限恢复、规划修订、ticket 验收范围和结束后事实同步。

`git diff --check` 通过。项目指南内联 JavaScript 使用 Node `new Function` 完成语法检查。没有改变 visualizer 的事件结构或 SQLite schema，因此未执行 visualizer 构建。

## 真实 smoke

在隔离临时 Git 仓库安装分发模板，使用真实 Codex 角色调用。质量命令明确配置为 `python3 -m unittest discover -v`，没有使用占位检查。规格为 `specs/counter-verified/spec.md`：实现整数递增函数，标准库 unittest 覆盖负数、零和正数。

| 运行 | 结果 | 实际证据 |
|---|---|---|
| `smoke-complete` | 15/15 阶段通过，session success | 规划 → 实现 → 3 项测试通过 → 独立审查批准 → 首次报告与 README 发布 → finish 后事实同步 |
| `smoke-evidence-empty` | 7/7 阶段通过，session success | 再执行 3 项测试；捕获确认 `empty: True`；独立复核 → 第二次报告 → 累计现状及新验收回执 |

两次成功执行关联同一 revision 1 和同一规格路径；第二次不修改 `counter.py` 或 `test_counter.py`。第二次业务工作流不自动提交文档，结果保留在工作区，符合该工作流的提交策略。

保留的报告：

- `executions/smoke-complete/document-3319c32310b5.md`，SHA-256：`6fa65643ae0d6da5518bd8506af3ffec8a1d147500415440cd01fd377dfa8f62`。
- `executions/smoke-evidence-empty/document-5095ec6eb0d3.md`，SHA-256：`76f786dc12ce5d939d5318774512ad25e13120dd40e5620e0bf1b8bd4c3e727b`。

最终核对两份报告与各自发布日志中的原文完全一致，README 与报告的所有本地链接存在；当前验收范围为整个规格、结果通过，回执快照与实现快照一致，失效提示为空。最后使用修正后的宿主索引逻辑重新核对工件，未重新调用 Agent，也未更改历史报告。

烟测工作区：`/private/var/folders/mn/rxr6_rgd1ls8rzfhrcl108qc0000gn/T/sssf-spec-smoke-y0yfp3b3`。原始审计在该目录的 `adws/adw_data/sssf.db` 与 `sessions/` 中；临时目录不作为长期证据存储。

保留了两次未计入成功验收的探测：`smoke-first` 因请求含未提供的人工证明，被 planner 明确报告为阻塞；`smoke-evidence` 暴露旧 diff 捕获的上一提交回退行为，已通过代码修复、回归测试和成功的空差异重跑关闭。最终页面核对发现的旧验收失效提示残留也已修复，并增加新回执刷新场景测试。

## 使用边界

Markdown 反映最近一次观察；无人运行工作流或刷新命令时，不自动追踪后续代码变更。跨工作树冲突仍需显式合并后重新核对，不能以最后写入者覆盖。文稿语义由角色判断，运行时不设置 Markdown 标题或表格门禁，也不代替业务 ADW 的验收责任。
