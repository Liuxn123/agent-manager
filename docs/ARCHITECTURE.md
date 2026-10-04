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

## 平台策略

UI 使用 Qt 标准布局与控件。进程调用传参数数组，不依赖 Windows 命令解释器。默认配置路径随平台变化；启动器只控制自己创建的 Popen 进程。

服务器 adapter 使用 Linux/systemd 固定程序，主机指纹校验不可关闭。服务器恢复尚不负责安装 runtime、安装服务或原地替换；这些是后续明确的能力，而非伪装成已成功恢复整套服务器。
