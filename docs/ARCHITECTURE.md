# v0.7.1 架构

今日视图的卡片与绘制代理位于 `ui/today_widgets.py`；列表采用统一委托，不为每行创建控件。`Daily.add_entry` 使用既有文件锁与原文比较追加任务 / 日程，`[高/中/低]`、`@HH:MM` 都是可人工编辑的可选 Markdown 标记，不升级 SQLite 或备份格式。专注计时使用单调时钟，只有手动开始时启动 1 秒 UI 定时器；不写入使用时长数据库。

产品入口：今日 → 项目 → Agent / 资源，数据保护按需进入。保留原工程、服务与 Adapter，不重建项目。

## 现有核心

- `domain.py` 的 Resource 仍是备份 / Agent 登记模型，id 与 options 保持兼容。
- `application.py` 统一调用 Adapter、资源 / 路径锁、备份、校验、恢复与演练。
- `adapters/`：本地 Hermes、服务器 Hermes、项目、Vault、通用 Agent。服务器代码仍通过严格 SSH 执行受限操作。
- `archives.py` / `snapshots.py` / `security.py`：原 .amb schema 1、AES-GCM、SQLite / JSONL 快照、凭据策略与路径防护。
- `project_workspaces.py`：projects.json schema 2，身份分配、管理锁、STATUS / TASKS / HANDOFF、笔记、索引、归档 / 重新启用。
- `portable.py`：配置中程序目录内的路径编码成 @portable，引导 U 盘移动后的解码；外部路径保持外部路径。

## 日常工作数据

`workbench.py` 提供 bounded Markdown IO、Daily、Catalog、frontmatter、阶段与关系字段、总结模板 / Prompt 接口、Obsidian URI。

| 对象 | 真源 | 程序保存什么 |
| --- | --- | --- |
| Daily | 每日/YYYY-MM-DD.md | 日期、日程与勾选任务直接在文件，不保存正文数据库副本 |
| Project | 原 projects.json + STATUS / TASKS / HANDOFF / 笔记 | STATUS 新增可选 phase、agents、resources；阶段总结追加 HANDOFF |
| Agent | 原 SQLite Resource 登记 + Adapter | 目录、入口、项目编号；备注使用资源库 Agent Markdown |
| Resource（资源库条目） | 资源/*.md 的 YAML frontmatter 与正文 | 唯一 id、类型、标签、说明、来源、路径、适用 Agent / 项目、备注、收藏和人工安装 / 配置 / 测试标记 |

资源库条目与原 `domain.Resource` 不混用：Catalog 使用独立 Markdown 索引模型，不改备份清单。手写文件无 id 时使用 file:相对路径身份；编辑后保持稳定。重复 id 与非法字段明确显示错误。

关系通过编号列表实现。Project STATUS 引用 Agent 登记 id、资源 id；资源的 agents 可引用登记 id 或类型，projects 引用项目编号；Agent options 可引用项目编号。无复杂关系表，无正文索引数据库。

SQLite 保留资源登记、程序设置、状态证据、任务结果。新增 workbench_schema=1 及可选 workbench_root 配置，最近使用仅保存时间与 id。使用 Store 既有路径编码；不增加正文表。

## 写入与外部编辑

每日 / 资源文件限制 1 MB、资源最多 1000 个、只扫描资源目录这一层。拒绝越界、符号链接 / junction 与非普通文件。写入使用独占锁、原文比较、临时文件与原子替换；新文件独占创建。外部编辑冲突明确拒绝覆盖。锁不会自动删除其他任务的锁。

项目仍走原 ProjectWorkspace 管理锁与 CAS 写入。阶段字段只修补管理字段，保留正文和未管理 frontmatter；总结走原 append_log。项目关联字段错误要求人工修复。

Qt 文件监听、短防抖、窗口重新激活与日期切换触发后台读取。路径监听按差异更新；今日可见时每两秒只读取今日文件，补足 macOS 原子保存的合并 / 缺失通知，不扫描项目。Daily / Catalog 读取不产生操作历史或正文持久化；Catalog 按文件 stat 复用索引。操作写入仍走 Worker、任务状态和 GUI 线程回调，正文结果不入日志。

## UI

一级堆栈仅六页，使用命名导航常量。`ui/workbench.py` 提供 TodayPage / CatalogPage / AgentPage；ProjectPage 扩展阶段概览、阶段总结、关联资料。

数据安全复用原 ResourcePage、备份历史 / 迁移、操作记录与保护概览。每日无备份主按钮。旧工作笔记从今日只读访问，原文件保留。

进程状态在数据安全保护概览中按需后台读取，单轮清单供所有 Agent 使用；隐藏页面停止轮询。任务列表按需加载摘要，选中才读详情；项目文档标签复用已加载正文。

## 增量迁移

启动通过 migrate_workbench 检查 workbench_schema：0→1 仅写配置标记，1 保持，未知未来版本拒绝打开。SQLite 表结构、旧登记 id、备份、凭据、projects.json 与正文不会被升级过程重写。

旧项目缺 phase 时从已有当前阶段或状态兼容显示；只有用户保存阶段 / 关系时写 STATUS。旧工作笔记和历史资源文件保留，不自动搬迁 / 删除。更换资料位置只保存引用路径，不隐式复制正文。

## 扩展边界

stage_prompt 是阶段总结生成接口的第一实现，提供上下文和可复制模板；不调用 AI、不自动执行文件内指令。后续可接入已有安全 Agent 调用，但须保持人工审阅与原文件写入保护。通用 Adapter 继续处理浅适配 Agent，不建立大量专用 UI 分支。

资源登记不构成 MCP Host、Skill 安装器或自动化调度器。原安全恢复确认及备份格式是稳定边界，日常 UI 改动不触及这些实现。
