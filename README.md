# Agent 管家

一个独立运行的 Python + PySide6 桌面项目，统一管理本地 Hermes、服务器 Hermes、本地项目和 Agent。关闭 Hermes 后仍然可以执行恢复。

## v0.4：规范项目管理

本地项目主页面沿用 `projects.json` v2、`P-YYYY-NNN-名称`、projects/archive 与 STATUS/TASKS/HANDOFF 规范。支持新工作区初始化、新项目初始化、状态与任务文档编辑、追加日志、归档和重新启用。移动前后核对文件，并更新 Obsidian 入口与生成索引。旧 L-归档保持只读；目录联接/符号链接、目标冲突及跨文件系统移动会阻断归档。原备份功能保留在“资料备份与 Obsidian”页签。操作失败保留现场与移动记录，不自动回滚或重试。

日常操作请看 [按模块使用说明](src/agent_manager/assets/docs/USER_GUIDE.md)。程序侧栏也提供“使用说明”，可离线阅读。设置页只展示备份位置、保留数量和口令库，维护选项放在默认折叠的“更多设置”。[同类工具调研与功能建议](docs/RESEARCH.md) 单独列出尚未实现的建议。

v0.3.1 简化设置页，并增加按模块跳转的离线使用说明。备份和恢复流程保持原有行为。

## v0.3：工作台与 U 盘便携版

日常使用集中在三个动作：**工作台查看状态 → 备份资料 → 需要时恢复**。工作台显示每项资料最近备份、校验、恢复演练和连接状态，以及最近七天的管家活动。资源页只保留常用按钮；校验、恢复演练、项目操作在“更多操作”中，技术参数在“配置与诊断”中。

运行包默认便携模式，整目录可放在 U 盘：

```text
AgentManager/
  AgentManager.exe       # Windows 启动入口
  _internal/            # 必须一起携带的运行库
  portable.json         # 便携模式标记
  data/                 # 登记、设置、任务、校验证据、可选加密口令库
  backups/              # 默认加密备份位置
  restored/             # 默认迁移恢复位置
```

macOS 的 `portable.json` 和 `data/` 位于 `.app` 旁边；Linux 与 Windows 使用程序目录。将整个目录复制到 U 盘，不要只复制可执行文件。程序内目录使用相对位置保存，移动目录或改变盘符后会自动跟随；电脑上的外部项目、Hermes、SSH 文件仍需在新电脑重新选择。每个系统使用对应构建，Windows 的 EXE 不能直接在 macOS/Linux 执行。关闭程序、等待任务完成后再拔出 U 盘。

备份口令可以临时输入；便携模式也可在**设置 → 解锁便携口令库**设置独立主口令，再在备份窗口勾选保存。`credentials.enc` 使用 scrypt + AES-GCM 加密，可以随 U 盘携带；程序关闭后重新解锁。不复制系统钥匙串，不自动复制 SSH 私钥。主口令需单独保管，无法从管家找回。

- **自动备份**：在资源编辑窗口开启“每天自动备份”。只在管家打开时执行，默认关闭；加密资料需要已保存口令，便携口令库需已解锁。检测到 Agent 正在运行会延后重试。服务器自动备份暂不启用；本地 Hermes 沿用原生备份流程与推送设置。
- **版本保留**：自动备份并完整校验成功后，才按设置保留最近 N 份。旧版本也必须能解锁且归属匹配才会清理；失败、口令已更换或不明归属的备份保留。Hermes 原生 Git 快照不使用此目录备份清理策略。
- **恢复演练**：选中加密备份执行“恢复演练”，在临时目录实际恢复，逐文件比较哈希，完成后清理临时资料；原目录不覆盖。证据随管家数据保存，区分“已校验”和“已演练”。每次正式恢复仍完整重新校验。
- **SQLite 快照**：其他 Agent 的打包备份使用 SQLite 只读在线备份接口，保存已提交数据，排除对应 WAL/SHM/Journal 辅助文件。每个数据库独立一致，不保证整个 Agent 的跨文件事务；普通文件发生变化会阻止备份，日常仍建议退出原应用。[SQLite 官方说明](https://sqlite.org/backup.html)。
- **本地记录搜索**：支持跨 Agent 搜索可阅读记录和 Hermes 会话标题，结果不写入活动记录。每次最多读取 300 个记录文件和 32 MiB；搜索不改原应用数据，也不搜索云端独有记录。

服务器配置新增明确的 **SSH 登录账号**、主机指纹文件和**读取已有服务器连接参数**按钮（复用 `HERMES_SERVER_*` 的公开路径与账号参数，不读取密码值）。检测分别报告 SSH、systemd 网关、CPU/内存/磁盘、备份工具与服务器口令文件。使用严格指纹校验；私钥不能代替登录账号。服务器备份文件校验不等于已经验证凭据解密，详情保留原生工具的实际报告。

Workbench 迁移范围见 [迁移说明](docs/WORKBENCH_MIGRATION.md)。此项目独立运行，不依赖原 Workbench 插件。

## Hermes 为主，也备份其他工作 Agent

“其他工作 Agent”支持 Codex、WorkBuddy、CodeBuddy、Claude Code 和手动选择目录的其他工具。一次备份同时包含**项目文件夹**与一个或多个**本地记录 / 配置目录**，加密为一个 `.amb` 文件。Codex 默认候选来自 `CODEX_HOME` / `~/.codex`；WorkBuddy 的候选来自其本机应用数据目录，最终以用户选定目录为准。

新电脑无需先导入配置：**工作台 → 换电脑恢复 → 输入口令 → 选择新文件夹 → 查看清单 → 恢复**。项目与记录可指定不同的子文件夹名称，全部校验后一次切换到总文件夹；恢复完成后自动登记为这台电脑的资料。既有登记保留，同一备份的恢复副本另行登记，不接管旧机器的启动程序。

点击**浏览本地记录**可阅读 JSONL 对话、Markdown、TXT 和 JSON 文件。登录材料不作为聊天记录展示；阅读内容不会写入管家的操作记录。SQLite 数据库保存为一致性快照，其他数据库保存为稳定文件；原应用可能需要重新登录、重新登记项目路径或通过其导入功能继续会话。管家不会修改未公开的数据库结构，云端专有记录应先从原应用导出，再添加导出目录。

备份前退出对应的 Agent，避免记录或数据库在复制过程中变化。记录目录默认排除缓存、日志、临时目录和 Codex 工作树；如需保存工作树项目，请单独选择它作为项目目录。数据库文件及登录文件如在选定目录内，会仅存入加密备份；系统钥匙串中的凭据仍需在新电脑重新登录。

目录位置参考：[Codex 官方说明](https://learn.chatgpt.com/docs/config-file/config-advanced#config-and-state-locations)、[WorkBuddy 常见问题](https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/FAQ)、[CodeBuddy 目录说明](https://www.workbuddy.cn/docs/cli/codebuddy-dir)。候选目录不是对所有客户端版本的记录导入保证。

## 初版功能

| 页面 | 已实现能力 |
|---|---|
| 本地 Hermes | 运行根和工具路径配置、备份状态、原生备份/推送、快照校验、完整恢复预览与执行、救援位置报告、备份提交列表、配置启动程序、启停本工具启动的进程 |
| 服务器 Hermes | SSH 连接、systemd 网关状态/启停/重启、服务器本机备份、校验、空目录恢复预览与执行；不自动启动恢复后的网关 |
| 本地项目 | 项目/知识库/Obsidian 登记、目录预览、Git 状态与安全快进拉取、打开 Vault、加密备份、恢复到新目录 |
| 其他工作 Agent | 类型预设、项目与多个记录目录一起备份、从文件直接恢复并登记、浏览文本记录、可选进程启停、适配器扩展接口 |
| 备份中心 | 当前本地 Hermes 快照与加密目录备份列表、从指定备份恢复；列表元数据与实际校验分开 |
| 任务记录 | 后台任务、结果、脱敏日志、安全检查点取消、启动后识别上次中断任务 |
| 设置 | 备份位置、合并导入/导出资源配置、运行依赖检测、系统凭据保存/移除 |

目录备份使用 AES-256-GCM + scrypt，直接将 ZIP 流加密，不生成明文 ZIP。包含工作文件、附件、选定目录设置和本地 Git 历史包。默认排除 `.git`、`.venv`、`venv`、`node_modules`、`__pycache__`、`.cache`、`.pytest_cache`；额外排除按目录名设置。普通文件（包括 `.env`）仅在加密备份中保存。

## 启动

### 下载运行包

私有仓库：[Agent Manager](https://github.com/Liuxn123/agent-manager)。登录后从 [版本下载页](https://github.com/Liuxn123/agent-manager/releases) 获取对应系统的运行包；最新测试构建也可在 Actions 的 **Desktop CI and packages** 中下载。包内有 README 与构建信息。

- Windows：解压整包后打开 `AgentManager/AgentManager.exe`。
- macOS：解压后打开 `AgentManager.app`；未签名构建需通过系统提供的“仍要打开”入口授权，不要求关闭系统保护。
- Linux：解压 `.tar.gz` 后运行 `AgentManager/AgentManager`，使用 tar 保留可执行权限。

运行包包含 Python 与 Qt，普通项目和 Agent 管理不要求另装 Python。Hermes 原生备份脚本仍需要可用的外部 Python 及其依赖；Git、GPG、SSH、Hermes 和 Obsidian 按使用的功能安装，并可在资源设置中指定路径。

### 从源码运行

需要 Python 3.11+ 和 Qt 支持的桌面系统；建议使用 Python 3.12。

Windows PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m agent_manager
```

macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m agent_manager
```

Windows 已建立环境后可双击 `start.cmd`。应用默认使用系统应用数据目录，不依赖源码位置。便携模式：

```text
agent-manager --data-dir /your/local/data
```

同一个数据目录只允许一个窗口。路径在本机配置，没有固定盘符、用户名、IP 或账号凭据。

## 首次配置

1. 在“本地 Hermes”页面选“从 Workspace 发现”，选择包含 `workspace.json` 的目录。确认后登记本地备份与项目；独立数据仓库仍保持自己的 Git 边界。
2. 在本地 Hermes 中核对运行根、备份仓库、工具 Python、受保护口令文件；首次恢复前执行校验。
3. 添加服务器，填写 SSH 别名、端口、远端目录、服务账号和服务范围。只填写私钥路径，不填写私钥内容。首次连接前通过受信任渠道核对指纹并建立 `known_hosts`；加密私钥先加载到 SSH Agent。
4. 登记 Obsidian、其他项目和 Agent 目录。在设置中选择独立的备份保存目录。
5. 项目/Agent 第一次备份输入独立口令，可选择保存到本机系统凭据存储；必须另存口令以便换电脑。

服务器功能目前针对 Linux + systemd。SSH 的 `python3` 与仓库中的服务器工具需已安装；使用 root 连接时，备份工具通过 `runuser` 切换到登记的服务账号。用户服务管理必须用对应账号登录。

Agent 启动用于非交互进程；终端交互型 CLI 请在终端使用。参数填写 JSON 字符串数组，例如 `["-m", "my_agent"]`，不通过 shell 拼接命令。停止按钮只控制本窗口启动的进程，不按 PID 接管外部 Agent；退出程序后进程可继续运行，但下次打开不能直接接管。

## 恢复与迁移

本地 Hermes：校验 → 恢复预览 → 核对目标并退出 Hermes → 确认恢复。执行通过原生备份锁与定时备份互斥，复用原生救援目录与数据库重建。完整恢复按快照精确镜像会话，可能移除当前独有会话；口令失败由原生工具在写入前拦截。

项目、Vault、通用 Agent：选择 `.amb` → 口令解锁 → 校验全部哈希 → 预览 → 恢复到新目录或空目录。目标非空则拒绝；内容先恢复到暂存目录，全部完成后切换。目录符号链接/联接排除并报告；备份与恢复目标拒绝重叠。

Git 本地提交保存为 `.agent-manager-history.bundle`；工作目录文件保持备份时版本。恢复仓库历史可另外执行：

```text
git clone /restored/path/.agent-manager-history.bundle /new/repository
```

随后对照恢复目录将工作文件复制到新仓库，保留未提交和未跟踪文件。初版不自动拼接 `.git` 与工作树，避免覆盖用户当前修改。子模块或嵌套仓库应分别登记，目录备份不会隐式备份每个嵌套仓库的本地 Git 历史。

服务器：校验 → 指定空目录 → 预览 → 确认恢复三个 home。数据恢复完成后仍需要安装 pinned runtime、检查并安装服务、验证知识库位置、确保旧网关已停后启用新网关。初版不提供生产服务器原地覆盖或自动回滚。

换电脑：复制 `.amb` 文件，直接使用“从备份文件恢复”；名称、目录范围和登记信息从解密后的清单取得，侧车 JSON 丢失不影响恢复。Hermes 原生备份仍从对应 Hermes 页面恢复。导出资源配置可另外保存登记清单；它不包含资料、系统凭据、SSH 私钥或 Git 登录。

## 数据与边界

- 发布包默认数据位置是程序旁的 `data/`；无 `portable.json` 的源码/本机模式使用 Windows `%LOCALAPPDATA%/AgentManager`、macOS `~/Library/Application Support/AgentManager`、Linux `${XDG_DATA_HOME:-~/.local/share}/agent-manager`。`--data-dir` 可覆盖。
- SQLite 只存资源配置、设置、任务结果、校验证据和脱敏日志。明文凭据字段拒绝保存；便携口令库或系统钥匙串保存备份口令，SSH/GPG 使用现有受保护材料。
- 命令失败不保存原始 stderr；敏感字段与常见令牌格式清洗后才写任务结果。
- 系统凭据存储不可用时明确报错，不回退到明文文件。
- `.amb` 首版单包限制 50 GiB / 20 万文件；需要更大目录可拆分登记。还原解密可能使用系统受限临时文件，结束时关闭清理。
- 请在外部写入程序退出后进行目录备份；本工具会检查复制期间文件是否变化，但不提供操作系统文件系统快照。
- 取消请求在安全点生效；原生恢复和关键写入不强杀。SSH 中断/超时仅表示未取得结果，需检查服务器实际状态后重试，不代表远端一定失败。
- 中断任务启动后标记为待检查，不自动重复恢复。后续会补远端持久任务 ID 和可恢复的检查点。

## 开发与打包

```text
python -m unittest discover -s tests -v
python -m compileall -q src scripts
python -m pip install -e ".[build]" -r requirements-build.txt
python scripts/build.py
```

构建会生成目录包及压缩包。CI 在 Windows x64、Linux x64、macOS Intel 和 Apple Silicon 上测试、构建并执行打包程序启动检查。不同系统分别打包，不能用 Windows exe 在 macOS/Linux 运行；其他硬件/旧操作系统受 Qt 与依赖支持范围限制。

Windows 构建会隔离 DLL 搜索路径，避免 PATH 中其他工具的 ICU/系统 DLL 混入。压缩包每次从全新暂存目录生成，并保留 macOS app 内部链接。Intel macOS 使用 `OPENSSL_STATIC=1` 从源码构建当前 cryptography（首次安装加 `--no-cache-dir --no-binary=cryptography`），避免 Homebrew OpenSSL 与 Python 自带库发生冲突；CI 已配置此步骤。启动检查直接解压并运行最终压缩包，而不是只检查构建目录。

UI 验证：`QT_QPA_PLATFORM=offscreen python -m agent_manager --data-dir /tmp/test-data --smoke-test --screenshot /tmp/window.png`。测试全部使用临时目录和测试进程，不连接生产服务器、不覆盖真实运行资料。

扩展与下一步见 [架构](docs/ARCHITECTURE.md) 和 [路线图](docs/ROADMAP.md)。
