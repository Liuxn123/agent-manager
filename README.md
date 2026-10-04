# Agent 管家

一个独立运行的 Python + PySide6 桌面项目，统一管理本地 Hermes、服务器 Hermes、本地项目和 Agent。关闭 Hermes 后仍然可以执行恢复。

## 初版功能

| 页面 | 已实现能力 |
|---|---|
| 本地 Hermes | 运行根和工具路径配置、备份状态、原生备份/推送、快照校验、完整恢复预览与执行、救援位置报告、备份提交列表、配置启动程序、启停本工具启动的进程 |
| 服务器 Hermes | SSH 连接、systemd 网关状态/启停/重启、服务器本机备份、校验、空目录恢复预览与执行；不自动启动恢复后的网关 |
| 本地项目 | 项目/知识库/Obsidian 登记、目录预览、Git 状态与安全快进拉取、打开 Vault、加密备份、恢复到新目录 |
| Agent 管理 | 引擎和目录登记、资料状态、非交互进程启停、加密资料备份与恢复、适配器扩展接口 |
| 备份中心 | 当前本地 Hermes 快照与加密目录备份列表、从指定备份恢复；列表元数据与实际校验分开 |
| 任务记录 | 后台任务、结果、脱敏日志、安全检查点取消、启动后识别上次中断任务 |
| 设置 | 备份位置、合并导入/导出资源配置、运行依赖检测、系统凭据保存/移除 |

目录备份使用 AES-256-GCM + scrypt，直接将 ZIP 流加密，不生成明文 ZIP。包含工作文件、附件、选定目录设置和本地 Git 历史包。默认排除 `.git`、`.venv`、`venv`、`node_modules`、`__pycache__`、`.cache`、`.pytest_cache`；额外排除按目录名设置。普通文件（包括 `.env`）仅在加密备份中保存。

## 启动

### 下载运行包

私有仓库：`https://github.com/Liuxn123/agent-manager`。仓库所有者登录后从 Actions 的 **Desktop CI and packages** 获取对应系统的构建产物。包内有 README 与构建信息。

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

1. 在总览选“从 Hermes Workspace 发现资源”，选择包含 `workspace.json` 的目录。确认后登记本地备份与项目；独立数据仓库仍保持自己的 Git 边界。
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

换电脑：导出资源配置，复制 `.amb` 或获取原生 Hermes 备份仓库；在新电脑导入并核对路径，再使用独立保存的口令解锁。系统凭据、SSH 私钥、Git 登录不随 JSON 配置导出。备份中心侧车 JSON 用于分类，丢失时仍可在对应资源中直接选择 `.amb` 恢复。

## 数据与边界

- 默认应用数据位置：Windows `%LOCALAPPDATA%/AgentManager`，macOS `~/Library/Application Support/AgentManager`，Linux `${XDG_DATA_HOME:-~/.local/share}/agent-manager`。
- SQLite 只存资源配置、设置、任务结果和脱敏日志。明文凭据字段拒绝保存；口令用操作系统安全凭据存储，SSH/GPG 使用现有受保护材料。
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

Windows 构建会隔离 DLL 搜索路径，避免 PATH 中其他工具的 ICU/系统 DLL 混入。压缩包每次从全新暂存目录生成。Intel macOS 使用 `OPENSSL_STATIC=1` 从源码构建当前 cryptography（首次安装加 `--no-cache-dir --no-binary=cryptography`），避免 Homebrew OpenSSL 与 Python 自带库发生冲突；CI 已配置此步骤。

UI 验证：`QT_QPA_PLATFORM=offscreen python -m agent_manager --data-dir /tmp/test-data --smoke-test --screenshot /tmp/window.png`。测试全部使用临时目录和测试进程，不连接生产服务器、不覆盖真实运行资料。

扩展与下一步见 [架构](docs/ARCHITECTURE.md) 和 [路线图](docs/ROADMAP.md)。
