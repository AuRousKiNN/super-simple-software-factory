# Codex SDK M4 实施报告

状态：M4 代码、确定性分发验收和活动集成清理已完成。
核对日期：2026-09-22。

## 已交付

- 执行入口已收敛为单一 `adw_modules/agents.py`。原先“旧实现保留在前、末尾再覆盖为
  Codex 实现”的结构已删除，独立的过渡模块也已移除。`agent_codex.py` 继续只负责 SDK
  生命周期，SDK 类型不传播到 ADW 脚本。
- 删除旧的 TypeScript 子代理扩展与主题文件；installer 不再分发扩展目录。planner/scout 使用
  M3 已验收的项目级 `sssf_recon` 原生 child role。
- 删除 `permissions.py` 中被后定义覆盖的旧变更计数实现，活动源码只保留 M2 内容级快照：
  tracked/untracked 内容、文件类型、权限位、symlink、Git index、仓库写锁和并发恢复保护。
  `data_dir` 不再有任何不可达的整目录放行实现。
- installer 升级为事务式分发器：
  - `.sssf/manifest.json` 记录分发版本、目标文件摘要、源摘要和 user/managed 所有权；
  - config、prompts、starter ADW、`quality.py`、`.env.sample`、`justfile` 被视为用户文件，重复
    安装和 `--force-managed` 都不覆盖；
  - managed 文件只在与上次 manifest 摘要一致时自动更新，本地改动在任何写入前报告冲突；
  - 每次有变化的安装先创建 `.sssf/backups/<snapshot>/`，覆盖所有待改路径、当前 config、
    SQLite 主文件及 WAL/SHM；写入使用同目录临时文件加 `os.replace`；中途失败自动恢复；
  - `--rollback latest` 或指定 snapshot 可恢复前一组分发文件、config 和 DB，若安装后任一捕获
    路径发生变化则拒绝回退；
  - manifest 和 snapshot 中的相对路径与 snapshot id 均防止绝对路径/父目录穿越，备份恢复
    区分普通文件、symlink 和不存在路径；
  - 已知旧文件只在写前快照后清理，空旧目录随之移除；`--force` 仅作为
    `--force-managed` 别名，不再表示无差别覆盖。
- 重复安装当前版本是稳定 no-op，不重写 manifest，也不额外生成 snapshot。
- `make_adw.py` 已改用 `run.finish()`；生成脚本精确锁定
  `openai-codex==0.155.1`、包含 `rich`，并通过真实 `uv run <generated> --help` 启动验收。
- README、Skill、cookbooks 和 references 已统一为 schema v2、直接 Codex model ID、CLI/API-key
  认证、thread/turn、结构化输出、child role、未知费用/上下文和当前安装/回退语义。

## 确定性验收

- Python 全套：`25 passed`。其中 M4 新增 6 组验收，覆盖：
  - 分发源只保留一个 Codex 执行入口，旧扩展不存在；
  - 空目录安装与 manifest 内容；
  - 重复安装无变化；
  - 既有 config/prompt 保留；
  - 自定义 managed 模块冲突在零部分写入时停止；
  - 合成写入故障自动恢复全部目标；
  - managed 强制更新后连同匹配 config/DB 回退；
  - 新生成 ADW 的依赖、`run.finish()`、Python 编译和 `uv --help` 启动。
- M1–M3 的配置、schema、会话、失败终态、权限、用量、DB、子代理和完整确定性 SDLC 回归全部
  同时通过。
- 聚焦 Ruff `F,E9`：scripts、ADW templates 和 tests 全部通过。
- Visualizer production build 通过：`vue-tsc --noEmit` 与 Vite 构建成功。
- Visualizer lint 通过并保留一条既有 `models.ts` `prefer-array-find` warning；没有 error。
- 精确残留搜索对 README 和活动 skill 源码无命中。installer 中保留的三个已知旧路径只用于在
  目标仓库执行一次性备份后清理，属于第 10.4 节要求的清理清单，不是可导入或可执行集成。
- 12 个 starter ADW 和生成器模板均精确声明相同 SDK 版本及 `rich`。

## 验收边界

- M4 没有执行真实模型 turn：分发、安装事务、生成器、文档和活动源码残留均可确定性验证，
  不需要新增数据外发或模型用量。真实单角色、逐 child role 标记与完整 SDLC 的授权状态继续沿用
  M1/M3 报告。
- 回退要求工作流已停止。若 DB、config 或任何被捕获文件在安装后继续变化，installer 会拒绝
  自动回退，避免覆盖新运行或工程师编辑；此时应先备份并显式处理差异。
- 全仓库未配置统一 Ruff 规则，直接启用 Ruff 全规则仍会报告既有格式、现代化和安全建议；M4
  采用与前序里程碑一致的 `F,E9` 正确性门。该结果不影响分发/清理验收，但若要把全规则 Ruff
  设为发布门，需要另行确定规则集并机械格式化存量代码。
