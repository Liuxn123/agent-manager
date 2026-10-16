# 小年助手 · v1.0

[下载 Windows 64 位免安装包（约 60 MB）](https://github.com/Liuxn123/agent-manager/releases/download/v1.0/AgentManager-1.0-win32-AMD64.zip) · [1.0 发布说明与校验文件](https://github.com/Liuxn123/agent-manager/releases/tag/v1.0)

完整解压后运行 `AgentManager/AgentManager.exe`，无需安装 Python；请保留整个程序目录。当前提供 Windows 包，具体测试范围与已知限制见发布说明。

**每天开始工作的个人工作台，用来管理今日计划、项目、知识资源和数据备份。**

本仓库维护小年助手的桌面软件源码、使用说明、测试与打包流程。项目资料、知识库、凭据、运行数据库和备份保存在用户本地，不随源码仓库上传。当前版本号为 1.0；版本改动见 [更新记录](docs/RELEASE_NOTES.md)，安装包验证范围见 [验证记录](docs/VALIDATION.md)。

核心工作区为今日工作台、计划、项目、知识库、数据安全和设置。新建项目采用四步引导，支持通用/自定义模板、开发规范、项目内 Skills 副本和知识资料关联；项目规则、任务与交接保持本地 Markdown。专用 Agent 管理暂缓，现有 Agent 数据保护能力继续由数据安全提供。

打开程序，先看今天做什么，再进入正在进行的项目并管理相关资料。项目规则、状态、交接与知识正文使用本地文件，可以由 Obsidian 编辑；工作台、计划和项目共用 SQLite 任务记录，项目任务文档可显式同步。备份与恢复集中在“数据安全”，“计划”入口集中查看跨日安排与中长期目标。

项目页采用左侧项目卡片、右侧项目工作区：先看当前阶段和下一步，再看目标、进展、阶段总结、待办、风险与最近记录。任务统计来自已有清单，完整原文和 Obsidian 入口始终保留；目录、归档及索引维护集中在“更多操作”。

项目归档 / 重新启用通过 STATUS 生命周期管理，资料目录保持原位；归档时隐藏登记的 Obsidian 项目入口，重新启用后恢复。项目状态保存到原 Markdown，任务与动态各自沿用既有数据来源。

## 模块入口

| 入口 | 解决什么问题 |
| --- | --- |
| 工作台（默认首页） | 今日任务、日程与 Todo，项目动态、提醒、最近使用和快捷入口；切换日期查看 Markdown |
| 计划 | 日计划、未来安排、长期目标、周期任务、项目时间线、模板和复盘；继续复用 Markdown |
| 项目 | 当前阶段与下一步、STATUS / TASKS / HANDOFF、阶段总结、笔记、只读 Git 状态、归档与重新启用 |
| 资源库 | 统一收藏、搜索和整理 Prompt / Skill / MCP / API / 工具 / 网站 / GitHub / 文章 / 模板等资料 |
| 工具整合 | 收藏常用软件和网站，登记本地程序或网页启动目标并一键打开 |
| 设置 | 配置 Markdown 与统一备份目录、备份保留数量、统一备份密码和浅色/深色主题；备份目录可指向本机网盘同步目录 |
| 数据安全 | 保护项目、任意文件夹、脚本输出、Obsidian、已登记 Agent 与 Hermes；归档类备份压缩加密并统一存放，支持校验、历史版本与恢复 |

资源库登记 MCP / API 的用途、接入说明和人工状态；不要在 Markdown 中保存令牌或密码。本版不连接 MCP / API，也不运行资源里的命令。阶段总结提供人工编辑和可复制 Prompt，不引入 AI 调度框架。

## 开始使用

Windows 便携包完整解压后运行 `AgentManager/AgentManager.exe`。保留整个目录；不要只复制 exe。macOS / Linux 使用对应平台运行包，源码模式也可运行。

```powershell
# 在 agent-manager 项目根目录执行
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:PYTHONPATH = (Join-Path $PWD "src")
.\.venv\Scripts\python.exe -m agent_manager
```

macOS / Linux：激活虚拟环境后 `python -m pip install -e . && python -m agent_manager`。Python 需要 3.11+。各平台分别打包，不能拿 Windows exe 在其他系统运行。

Windows 源码启动可双击 `start.cmd`，会直接进入“工作台”；当项目旁已有 AgentManager-Portable 安装时沿用其 data，避免打开另一套空登记。需要隔离开发时显式设置 AGENT_MANAGER_DATA_DIR。命令行启动默认也进入“工作台”，可通过 `--startup-page plan` 进入“计划”。

第一次打开：

1. 在“项目”选择已有规范工作区（含 `agent/projects.json`），或初始化一个空文件夹。
2. 在“工作台”切换“任务 / 日程 / Todo”查看当天安排，勾选任务后写回日期对应的 Markdown；顶部日期框 / 左右箭头切换日期，“回到今天”返回今天。外部修改 Markdown 后点“刷新”载入最新内容。进入“计划”可查看日历、周/月安排和目标；周期任务只在计划中投影，点击“加入每日计划”后才写入对应日期的 Markdown。模板可套用到选定日期。
3. 进入项目查看阶段与下一步；新建时依次填写信息、调整开发规范、选择技能与资料并确认创建。
4. 在“资源库”收藏说明与 Prompt。
5. 需要保护资料时进入“数据安全”，管理备份登记、口令与恢复。

## Markdown 与 Obsidian

有工作区时，默认文字资料放在 `myself/Agent工作台/`；没有工作区时放在管家数据目录的 `工作台/`。设置可选择另一个真实文件夹；选择 Obsidian 知识库内的目录即可共用。

```text
Agent工作台/
  每日/2026-10-05.md
  资源/<唯一编号>.md
```

每日文件使用 `# 日期`、`## 日程`、`## 今日任务`，任务为 `- [ ]` / `- [x]`，也兼容普通项目符号。资源使用 YAML frontmatter 与 Markdown 正文。项目阶段和关联 ID 可选地写入已有 STATUS，阶段总结追加到 HANDOFF，不创建重复的 SUMMARY / PROGRESS / REFLECTION 文件。

外部修改会自动刷新。保存时比较原文件版本；若 Obsidian 已修改，拒绝覆盖并提示刷新。管家不创建或修改 `.obsidian` 配置。

## 数据兼容与安全

- 原 `projects.json` schema 2、项目编号、目录、STATUS / TASKS / HANDOFF / 笔记、Agent 登记和 SQLite 配置继续使用。
- v0.7 只增加 `workbench_schema=1` 配置标记；启动不会批量改写项目或搬动个人文件。旧工作笔记保留，并可在“今日 → 旧版工作笔记”查看。
- `.amb` 仍使用原 schema 1 / AES-GCM 加密格式，保护聊天、配置、附件与工作文件；不生成明文中间 ZIP。SQLite 备份包括已提交的 WAL 数据。
- 恢复先预览、校验再确认；.amb 恢复只写入新目录或空目录。恢复演练使用临时目录，不覆盖原资料。
- Hermes 继续使用原生备份与恢复工具；服务器 SSH 严格验证主机指纹，不自动连接、恢复或启动网关。
- 项目 Git 不自动检测；myself 的 Git 管理需明确开启。专用 Agent 备份仓库只整理已经校验的加密副本。
- U 盘内的程序配置路径随便携目录移动；外部项目 / 知识库 / SSH 私钥仍需自行携带或重新选择。资料 Markdown 不会被当作应用数据库正文保存。

资料文件夹在 U 盘外时，移动程序本身不会携带它。可把资料文件夹登记为 Obsidian / 项目备份，或在关闭相关应用后复制整个资料文件夹。原应用的登录、运行环境与能否续聊，仍需在目标电脑验证。

## 验证与打包

```powershell
.venv\Scripts\python -m unittest discover -s tests -q
.venv\Scripts\python scripts/build.py
.venv\Scripts\python scripts/smoke_package.py
```

测试覆盖加密恢复、错误口令 / 篡改 / 路径边界、SQLite / JSONL 快照、迁移登记、便携路径、项目归档、Obsidian 联动、每日任务、资源编辑冲突、阶段总结与界面衔接。CI 为 Windows、Linux、macOS Intel / Apple Silicon 分别构建。

## 文档

- [分模块使用说明](src/agent_manager/assets/docs/USER_GUIDE.md)
- [恢复范围与限制](src/agent_manager/assets/docs/RESTORE_SCOPE.md)
- [架构与数据关系](docs/ARCHITECTURE.md)
- [路线图](docs/ROADMAP.md)
- [版本说明](docs/RELEASE_NOTES.md)
- [性能检查](docs/PERFORMANCE.md)
- [本机验证记录](docs/VALIDATION.md)

不提供邮件、云盘或完整日历客户端，不开发完整 MCP Host、工作流引擎、多 Agent 自动编排、浏览器自动化或 Obsidian 替代品。扩展以实际日常使用需求为依据。
