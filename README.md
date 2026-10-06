# Agent 管家 · v0.7.8

**每天开始 Agent 工作的个人工作台，用来管理今日工作、项目、Agent、Prompt/Skills/MCP 等资源，并保护重要 Agent 数据。**

打开程序，先看今天做什么，进入正在进行的项目，再找到需要的 Agent 和资源。每日计划、项目记录与资源正文保存在真实 Markdown 文件中，可以由 Obsidian 编辑；备份与恢复集中在“数据安全”。今日首页先显示“今日要做”，再显示可设截止日期的 Todo；两块清单共用每日 Markdown，编辑计划后自动刷新。日程总览仍可查看完整跨日安排。

项目页采用左侧项目卡片、右侧项目工作区：先看当前阶段和下一步，再看目标、进展、阶段总结、待办、风险与最近记录。任务统计来自已有清单，完整原文和 Obsidian 入口始终保留；目录、归档及索引维护集中在“更多操作”。

v0.7.5 修复 Windows 下文件监听阻止归档 / 重新启用的问题。移动期间释放管家监听，结束后恢复；失败记录区分未移动与需要核对的中断。旧 `L-` 归档可通过“从旧归档继续”初始化新编号项目，原资料保留原位。

## 六个入口

| 入口 | 解决什么问题 |
| --- | --- |
| 今日（默认首页） | 默认看今天；切换任意日期，日程总览查看未来安排与所有未完成任务；继续项目与异常提醒 |
| 项目 | 当前阶段与下一步、STATUS / TASKS / HANDOFF、阶段总结、笔记、归档与重新启用 |
| Agent | Hermes、Codex、Claude Code、WorkBuddy、CodeBuddy 和手动登记 Agent 的常用入口、记录、目录、关联资源 |
| 资源库 | 统一收藏、搜索和整理 Prompt / Skill / MCP / 工具 / 网站 / GitHub / 文章 / 模板等资料 |
| 数据安全 | 原生 Hermes 备份恢复、加密 .amb、完整性校验、恢复演练、备份历史、换电脑恢复与操作记录 |
| 设置 | 文字资料位置、备份位置和必要选项 |

资源库登记 MCP 的用途、安装与配置说明、安装 / 配置 / 测试状态；本版不连接 MCP，也不运行资源里的命令。阶段总结提供人工编辑和可复制 Prompt，不引入 AI 调度框架。

## 开始使用

Windows 便携包完整解压后运行 `AgentManager/AgentManager.exe`。保留整个目录；不要只复制 exe。macOS / Linux 使用对应平台运行包，源码模式也可运行。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -m agent_manager
```

macOS / Linux：激活虚拟环境后 `python -m pip install -e . && python -m agent_manager`。Python 需要 3.11+。各平台分别打包，不能拿 Windows exe 在其他系统运行。

Windows 源码启动可双击 `start.cmd`；当项目旁已有 AgentManager-Portable 安装时沿用其 data，避免打开另一套空登记。需要隔离开发时显式设置 AGENT_MANAGER_DATA_DIR。

第一次打开：

1. 在“项目”选择已有规范工作区（含 `agent/projects.json`），或初始化一个空文件夹。
2. 在“今日”使用 Todo 管理有截止日期的待办，或在“今日要做”添加当天任务；任务勾选后写回日期对应的 Markdown。顶部日期框 / 左右箭头切换日期，“回到今天”返回今天。“日程总览”集中查看未来 30 天、所有未完成任务、全部已登记事项或自选日期范围；双击进入对应日期。需要整体整理时点“编辑今日计划 / 编辑当日计划”。
3. 进入项目查看阶段与下一步，必要时添加关联 Agent 和资源。
4. 在“Agent”登记常用工具，在“资源库”收藏说明与 Prompt。
5. 需要保护资料时进入“数据安全”，沿用已有备份登记、口令与恢复流程。

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
