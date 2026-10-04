# 架构与扩展约定

## 模块边界

```text
Qt pages / dialogs
    → ApplicationService / Worker
    → domain Resource / RestorePlan / AdapterRegistry
    → adapters local / server / projects / agents
    → filesystem / native Hermes CLI / OpenSSH / operating-system keyring
```

`storage.Store` 每次调用使用独立 SQLite 连接，适合 Qt 工作者线程。窗口只读取已有记录；目录扫描、CLI、SSH、加解密在任务线程完成。工作者通过 Qt 信号返回 GUI 线程，禁止直接访问 QWidget。

恢复计划与资源 ID、来源、目标及备份哈希绑定；执行时再次核对。应用内部锁按资源、服务器和目录重叠互斥。本地 Hermes 恢复还复用原生 `BackupRunLock`；初版要求原生工具提供这一接口。

## 添加 Agent / 资源能力

1. 在 `domain.KINDS` 登记资源类型与显示名称。
2. 实现适配器（不依赖 Qt）：`capabilities`、`observe` 及所声明的方法。
3. 在 `application.build_registry()` 注册。重复类型拒绝覆盖。
4. 在 UI 添加表单与页面或复用通用资源页面；耗时操作必须调用 `MainWindow.submit`。
5. 新增写操作需定义参数验证、锁、成功回证、中断/失败行为；恢复需要 RestorePlan 与目标检查。
6. 为实际行为添加临时目录或隔离进程测试。只有在对应环境演练后开放生产写能力。

首版是显式可信模块登记，不动态执行用户配置中的 Python 源码，不从网络下载插件。以后需要第三方模块时再设计发现、版本兼容和启用策略。

## 备份格式

Hermes 继续使用原有 V3 snapshot 和 GPG，不新建 Hermes 会话格式。目录资源使用版本化 `.amb`：magic、随机 salt/nonce、AES-GCM 密文 ZIP、认证 tag。清单含文件名、SHA-256、大小、普通权限、空目录和排除项。

ZIP 直接流入加密 writer，完成并 fsync 后将 `.partial` 重命名为 `.amb`。公共侧车 JSON 仅提供资源 ID、时间和数量，不能代替认证与哈希检查。解密后拒绝重复、越界、不兼容文件名、链接、未声明文件和大小异常。

数据库与资源导出均版本化；引入 schema 变更时添加事务迁移，不能直接覆盖旧库。敏感信息不进入迁移配置或版本管理。

v0.3 增加独立 evidence 表保存观测、校验、恢复演练与调度时间，旧表和任务原样保留。`maintenance` 提供工作台与调度共同使用的保护状态；快速列表以文件大小和修改时间判定上次证据是否仍适用，正式恢复总是重新认证并检查哈希。恢复演练调用同一 RestorePlan 与恢复实现，在内部临时根下逐文件校验并清理。

便携包使用程序旁 `portable.json`，不依赖工作目录。Store 对便携根内的路径编码为 `@portable/`，读取时按当前根解码并拒绝越界；外部目录保持原始路径，由用户在新电脑确认。口令库为独立 AES-GCM 密文文件，主口令经 scrypt 派生，仅在内存解锁。发布构建不带任何本机配置或凭据。

Agent 多目录打包使用 `snapshots.snapshot_file` 读取 SQLite 一致性快照，排除对应辅助文件。普通文件仍检查读取前后大小和修改时间；快照只保证单个数据库一致，不声明多个数据库和外部文件的应用级原子性。调度器在 Qt 定时器中只提交后台任务，不在 GUI 线程扫描进程、加密或访问远端。

## 平台策略

`project_workspaces.ProjectWorkspace` 管理项目生命周期，直接读写既有 `agent/projects.json` v2 和项目管理文档，不在 SQLite 复制项目状态。与原 PowerShell 工具共用 `.manage.lock`；编号先永久保留，再创建项目。新建优先使用用户工作区模板。UI 的项目管理页与旧备份页在同一模块的两个页签中并存。管理读操作不将文档内容持久化到管家任务历史。

归档/重新启用预览绑定登记表、项目文件和目录清单；执行前重新核对，采用同文件系统目录重命名。关键切换阶段不取消，移动后验证所有文件，再更新管理文档、已验证入口和索引生成区块；人工区块不覆盖。任一中间步骤失败保留移动记录与现场，禁止盲目重试。Windows 只删除经核对的联接本身，不递归删除目标；macOS/Linux 使用目录符号链接作为 Obsidian 入口。

UI 使用 Qt 标准布局与控件。进程调用传参数数组，不依赖 Windows 命令解释器。默认配置路径随平台变化；启动器只控制自己创建的 Popen 进程。

服务器 adapter 使用 Linux/systemd 固定程序，主机指纹校验不可关闭。服务器恢复尚不负责安装 runtime、安装服务或原地替换；这些是后续明确的能力，而非伪装成已成功恢复整套服务器。
