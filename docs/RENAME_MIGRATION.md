# 升级到 selfManger 1.0.1

产品、源码目录、Python 包、启动命令和发布包统一使用 `selfManger`（保留此拼写）。项目配置为 `.selfManger/config.json`，技能副本为 `.selfManger/skills/`，任务管理区使用 `selfManger:tasks` 标记。

应用只读取新名称：`SELFMANGER_DATA_DIR`、系统数据目录 `selfManger` 和凭据服务 `selfManger`，不自动回退旧目录或旧变量。已有加密归档格式不因品牌变化重写，历史 Release 保留原附件名。

旧数据需在关闭旧程序后一次性迁移。源码提供独立工具，默认仅检查，增加 `--apply` 才执行；不会在应用启动时自动运行。

```powershell
python scripts/migrate_legacy_data.py --source "$env:LOCALAPPDATA/AgentManager" --destination "$env:LOCALAPPDATA/selfManger" --snapshot 'D:/migration-snapshot' --workspace 'D:/workspace' --system-credentials
```

确认目录正确、目标与快照不存在后，再给同一命令增加 `--apply`。工作区参数用于迁移已登记项目中的配置目录和生成标记；系统凭据参数仅迁移此应用服务的备份密码及 Git 令牌，不将明文写入文件。工具会保存数据/管理文件快照并校验文件摘要、数据库完整性和各表数量。存在冲突或链接则停止，不覆盖目标。

工作台文字资料单独保留原始字节并迁移：

```powershell
python scripts/migrate_legacy_data.py --directory-only --source 'D:/workspace/myself/Agent工作台' --destination 'D:/workspace/myself/selfManger' --snapshot 'D:/notes-snapshot' --apply
```

便携包默认使用程序旁的 `data`。要使用已迁移的本机数据，运行 `selfManger.exe --data-dir "$env:LOCALAPPDATA/selfManger"`，或设置 `SELFMANGER_DATA_DIR`；不要同时运行两份程序操作同一数据目录。自定义目录和其他机器需分别核对，不会扫描整台电脑或静默迁移。
