# Agent 管家 v0.8.2 架构

v0.8.2 资源库页面重排为筛选 / 列表 / 详情三栏，筛选状态仍来自 `Catalog` 扫描的 Markdown frontmatter，不增加 SQLite 正文或索引字段；设置页以卡片展示资料目录、备份目录和密码状态，既有保存处理与数据路径保持不变。

v0.8.0 项目归档 / 重新启用通过带管理锁的 `ProjectWorkspace.set_archived` 修改 STATUS 状态，不移动项目目录、不改变 projects.json 路径或 Obsidian 联接。Frontmatter 的 `archived_from` 暂存归档前状态，重新启用时恢复；旧归档缺少该字段时回到 active。项目归类、索引和工作台过滤均读取 STATUS lifecycle，因此兼容仍位于旧 `archive/` 目录的编号项目。旧移动方法保留供兼容，不再由此 UI 流程调用。

v0.8.0 项目状态概览将卡片操作直连真实数据源：目标 / 进展 / 风险编辑原 `agent/STATUS.md`，下一步编辑原 `agent/TASKS.md`，总结与日志继续走 HANDOFF 追加流程；状态标题另提供完整 STATUS 编辑入口。写入仍使用既有 CAS 保存，未创建第二份正文。

v0.7.9 侧栏首页显示为“工作台”。TodayPage 首次展示时加载，之后不监听文件变化、不响应窗口激活自动刷新，也不做日期 / 文件轮询；手动刷新按钮重读资料。应用内任务写入仍通过原成功回调刷新页面。其他 Markdown 页面保留各自的监听策略。

v0.7.8 今日页移除优先级编辑、优先级统计和专注计时。编辑计划仍写入当前日期 Markdown，保存回调刷新今日任务及对应日期的 Todo；保留每日文件、原子保存和冲突保护。

v0.7.7 仅调整 TodayPage 左栏任务卡片顺序：先“今日要做”，后 Todo；数据与交互保持不变。

v0.7.6 TodayPage 首页只保留 Todo 与当日任务。Todo 复用 Agenda 对每日 Markdown 的只读索引，以任务所在日期作为截止日；新增 Todo 仍调用 Daily.add_entry 写入日期文件的“今日任务”区。双击待办会切换到对应日期，由原任务清单负责勾选和冲突保护。没有新增 SQLite 或 projects.json 字段。完整日程仍在二级日程总览和 Markdown 编辑器中管理。

项目表现层位于 `ui/project_widgets.py`：绘制项目卡片，基于已加载 Markdown 提取状态概览、真实复选框 / 表格任务、日期日志与阶段总结摘要。宽度决定三 / 二 / 一栏；外层滚动保护小窗口操作。完整原文保留在同一组标签，菜单复用原 ProjectPage 操作和禁用规则。解析跳过围栏代码块示例；概览限长，未知章节仍可读原文。没有新增数据库字段、文件格式或后台轮询，切换标签 / 卡片原文不重新读取。

今日视图的卡片与绘制代理位于 `ui/today_widgets.py`；列表采用统一委托，不为每行创建控件。`Daily.add_entry` 使用既有文件锁与原文比较追加任务 / 日程。读取时仍兼容旧 Markdown 中的优先级标记，但今日界面不再新增或突出显示该标记；不升级 SQLite 或备份格式。

产品入口：工作台 → 项目 → Agent / 资源，数据保护按需进入。保留原工程、服务与 Adapter，不重建项目。

## 现有核心

- `domain.py` 的 Resource 仍是备份 / Agent 登记模型，id 与 options 保持兼容。
- `application.py` 统一调用 Adapter、资源 / 路径锁、备份、校验、恢复与演练。
- `adapters/`：本地 Hermes、服务器 Hermes、项目、Vault、通用 Agent。服务器代码仍通过严格 SSH 执行受限操作。
- `archives.py` / `snapshots.py` / `security.py`：原 .amb schema 1、AES-GCM、SQLite / JSONL 快照、凭据策略与路径防护。
- `project_workspaces.py`：projects.json schema 2，身份分配、管理锁、STATUS / TASKS / HANDOFF、笔记、索引；归档 / 重新启用优先通过 STATUS 生命周期字段处理。
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

Windows 原子替换遇到短暂读句柄占用（5 / 32 / 33）时，后台最多等待 250 ms 重试；每次重新检查安全路径与原文，期间有外部修改仍拒绝覆盖。其他权限错误直接报告，不绕过文件权限。

项目仍走原 ProjectWorkspace 管理锁与 CAS 写入。阶段字段只修补管理字段，保留正文和未管理 frontmatter；总结走原 append_log。项目关联字段错误要求人工修复。

Qt 文件监听、短防抖、窗口重新激活与日期切换触发后台读取。路径监听按差异更新；今日可见时每两秒只读取今日文件，补足 macOS 原子保存的合并 / 缺失通知，不扫描项目。Daily / Catalog 读取不产生操作历史或正文持久化；Catalog 按文件 stat 复用索引。操作写入仍走 Worker、任务状态和 GUI 线程回调，正文结果不入日志。

## UI

v0.7.5 ProjectPage 在目录移动前暂停窗口拥有的 QFileSystemWatcher（包括隐藏 Today 的项目监听）。watch / update_watches 在暂停标记期间拒绝迟到读取回调注册路径；MainWindow 的 task_completed 信号在任务结果处理结束后统一释放暂停，成功后按新路径刷新，失败 / 取消同样恢复。后台业务移动仍由既有 Worker、资源锁与工作区锁执行。旧 L-归档只使用既有 create 初始化接续项目，历史目录保持原位；不增加登记字段。

移动回执兼容旧 prepared / completed，失败新增 failed_before_move（未 rename 且来源清单一致）或 needs_review，并记录 phase、error_type、error、winerror；不自动回滚已移动目录。

v0.7.4 复用 Daily 按日期文件作为唯一真源。Agenda 是只读跨日期索引，按 stat 缓存、限制文件 / 总大小 / 显示条数、逐文件报告错误；只在打开总览或文件事件 / 手动刷新时后台读取，不增加全局轮询。ui/agenda.py 提供总览二级弹窗，打开日期后仍使用 TodayPage 的原编辑 / 勾选与 CAS 写入。未完成任务不自动改日期。

TodayPage 区分系统今日与当前浏览日期，跨午夜只推进原本停留在今天的视图；后台返回必须匹配日期与当前资料路径，避免快速切换日期后旧响应显示为新日期。添加 / 编辑捕获目标日期、路径与原文，写入仍走原 Worker。新增任意日期选择不改变 Markdown / SQLite / projects.json / .amb schema。

项目详情逐份读取管理文档，错误作为可见状态返回，避免 TASKS 缺失造成 STATUS / HANDOFF 一并空白。文档错误控制依赖它的编辑操作。CatalogPage.set_scope 统一清除遗留筛选，新建资源沿用当前关联；Agent 备注写入同一 Markdown 后导航到结果。打开系统目录返回实际成功状态，失败不记录使用成功。源码启动脚本显式选用相邻便携安装的数据，不合并或自动迁移其他数据目录。


一级堆栈仅六页，使用命名导航常量。`ui/workbench.py` 提供 TodayPage / CatalogPage / AgentPage；ProjectPage 扩展阶段概览、阶段总结、关联资料。

数据安全复用原 ResourcePage、备份历史 / 迁移、操作记录与保护概览。每日无备份主按钮。旧工作笔记从今日只读访问，原文件保留。

进程状态在数据安全保护概览中按需后台读取，单轮清单供所有 Agent 使用；隐藏页面停止轮询。任务列表按需加载摘要，选中才读详情；项目文档标签复用已加载正文。

## 增量迁移

启动通过 migrate_workbench 检查 workbench_schema：0→1 仅写配置标记，1 保持，未知未来版本拒绝打开。SQLite 表结构、旧登记 id、备份、凭据、projects.json 与正文不会被升级过程重写。

旧项目缺 phase 时从已有当前阶段或状态兼容显示；只有用户保存阶段 / 关系时写 STATUS。旧工作笔记和历史资源文件保留，不自动搬迁 / 删除。更换资料位置只保存引用路径，不隐式复制正文。

## 扩展边界

stage_prompt 是阶段总结生成接口的第一实现，提供上下文和可复制模板；不调用 AI、不自动执行文件内指令。后续可接入已有安全 Agent 调用，但须保持人工审阅与原文件写入保护。通用 Adapter 继续处理浅适配 Agent，不建立大量专用 UI 分支。

资源登记不构成 MCP Host、Skill 安装器或自动化调度器。原安全恢复确认及备份格式是稳定边界，日常 UI 改动不触及这些实现。
