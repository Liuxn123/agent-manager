from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from ..archives import canonical_system_path, is_link
from ..domain import Resource, RestorePlan, UserError
from ..runtime import TaskContext, json_result, run_process


def required_directory(options: dict, key: str) -> Path:
    value = str(options.get(key, "")).strip()
    if not value:
        raise UserError("请先配置资源目录。")
    path = Path(value).expanduser()
    if not path.is_dir() or is_link(path):
        raise UserError("配置的目录不存在或是链接，请检查资源设置。")
    return path.resolve()


def snapshot_token(repo: Path) -> str:
    snapshot = repo / "snapshot"
    if not snapshot.is_dir() or is_link(snapshot):
        raise UserError("没有可恢复的快照。")
    digest = hashlib.sha256()
    for path in sorted(snapshot.rglob("*")):
        if is_link(path):
            raise UserError("快照包含链接，已拒绝操作。")
        if path.is_file():
            digest.update(path.relative_to(snapshot).as_posix().encode("utf-8"))
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


class LocalHermesAdapter:
    capabilities = frozenset({"observe", "backup", "verify", "restore", "open", "versions", "start", "stop", "library"})

    def __init__(self) -> None:
        from .agents import AgentAdapter
        self.processes = AgentAdapter()

    def _launcher(self, resource: Resource) -> Resource:
        return Resource(resource.name, "agent", {**resource.options, "path": resource.options.get("home", "")}, resource.id)

    def start(self, resource: Resource, context: TaskContext) -> dict:
        return self.processes.start(self._launcher(resource), context)

    def stop(self, resource: Resource, context: TaskContext) -> dict:
        return self.processes.stop(self._launcher(resource), context)

    def running_ids(self) -> list[str]:
        return self.processes.running_ids()

    def library(self, resource: Resource, context: TaskContext) -> dict:
        from ..hermes_library import list_library
        return list_library(resource, context)

    def _base(self, resource: Resource) -> tuple[list[str], Path, Path]:
        repo = required_directory(resource.options, "backup_repo")
        configured_home = str(resource.options.get("home", "")).strip()
        if not configured_home:
            raise UserError("请配置 Hermes 运行目录。")
        home = canonical_system_path(Path(configured_home))
        if is_link(home) or any(is_link(parent) for parent in home.parents) or (home.exists() and not home.is_dir()):
            raise UserError("运行目录不安全，不能使用链接或文件作为运行根。")
        home = home.resolve()
        if home == Path(home.anchor) or home.is_relative_to(repo) or repo.is_relative_to(home):
            raise UserError("Hermes 运行根必须与备份仓库分开，且不能是磁盘根目录。")
        script = repo / "tools/hermes_backup.py"
        if not script.is_file():
            raise UserError("备份仓库缺少 tools/hermes_backup.py，请选择现有 Hermes 备份仓库。")
        python = str(resource.options.get("python", "")).strip() or ("python3" if getattr(sys, "frozen", False) and os.name != "nt" else "python" if getattr(sys, "frozen", False) else sys.executable)
        return [python, str(script), "--repo", str(repo)], repo, home

    def _env(self, home: Path) -> dict[str, str]:
        return {**os.environ, "HERMES_HOME": str(home), "PYTHONUTF8": "1"}

    def _check_previous_task(self, home: Path) -> None:
        state = home / "cache/hermes-local-backup-state.json"
        if state.is_file():
            if state.stat().st_size > 100_000:
                raise UserError("原生备份状态文件异常，请先检查现有备份任务。")
            try:
                report = json.loads(state.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise UserError("无法读取原生备份状态，请先检查。") from exc
            if not isinstance(report, dict) or report.get("status") == "running":
                raise UserError("原生备份仍在运行或上次任务中断，请核对后再执行备份/恢复。")

    def observe(self, resource: Resource, context: TaskContext) -> dict:
        _, repo, home = self._base(resource)
        metadata = repo / "snapshot/snapshot.json"
        report: dict = {"home": str(home), "home_present": home.is_dir(), "backup_repo": str(repo), "snapshot_present": metadata.is_file()}
        if metadata.is_file() and metadata.stat().st_size < 20_000_000:
            data = json.loads(metadata.read_text(encoding="utf-8"))
            report.update(created_at=data.get("created_at"), counts=data.get("counts", {}), format=data.get("format"))
        try:
            report["git_status"] = run_process(["git", "-C", str(repo), "status", "--porcelain"], context, timeout=30).strip() or "工作区干净"
            report["branch"] = run_process(["git", "-C", str(repo), "branch", "--show-current"], context, timeout=30).strip()
        except UserError:
            report["git_status"] = "Git 状态读取失败，请检查 Git 安装与仓库权限"
        report["runtime_state"] = "本工具启动的进程运行中" if resource.id in self.running_ids() else "没有本工具管理的进程；外部进程状态未知，恢复时由备份工具检查"
        return report

    def versions(self, resource: Resource, context: TaskContext) -> dict:
        _, repo, _ = self._base(resource)
        output = run_process(["git", "-C", str(repo), "log", "-20", "--format=%H %cI", "--", "snapshot"], context, timeout=30)
        return {"versions": [{"commit": line.split()[0], "time": line.split()[1]} for line in output.splitlines() if len(line.split()) == 2],
                "note": "首版恢复当前 snapshot；历史提交可在独立副本检出、校验后登记为备份仓库。"}

    def verify(self, resource: Resource, context: TaskContext) -> dict:
        command, _, home = self._base(resource)
        result = json_result(run_process(command + ["verify", "--node", "local"], context, env=self._env(home)))
        if result.get("valid") is not True:
            raise UserError("快照未通过校验，已阻止恢复。")
        return result

    def backup(self, resource: Resource, context: TaskContext) -> dict:
        command, _, home = self._base(resource)
        if not home.is_dir():
            raise UserError("运行目录尚不存在；请先恢复或选择已有运行目录。")
        self._check_previous_task(home)
        arguments = ["backup", "--node", "local", "--home", str(home)]
        if resource.options.get("passphrase_file"):
            arguments += ["--passphrase-file", str(Path(resource.options["passphrase_file"]).expanduser())]
        if not resource.options.get("push", True):
            arguments.append("--no-push")
        # Never kill native backup in its snapshot replacement phase.
        return json_result(run_process(command + arguments, context, env=self._env(home), cancellable=False, timeout=1800))

    def plan_restore(self, resource: Resource, context: TaskContext) -> RestorePlan:
        self.verify(resource, context)
        command, repo, home = self._base(resource)
        options = ["restore", "--node", "local", "--full", "--home", str(home)]
        if resource.options.get("workspace"):
            options += ["--workspace", str(Path(resource.options["workspace"]).expanduser())]
        preview = json_result(run_process(command + options, context, env=self._env(home)))
        preview["note"] = "完整恢复会精确镜像会话，当前独有会话可能被移除；执行前退出 Hermes Desktop 和网关。已有工具会保存救援资料。"
        return RestorePlan(resource.id, snapshot_token(repo), str(repo), str(home), preview)

    def restore(self, resource: Resource, plan: RestorePlan, context: TaskContext) -> dict:
        command, repo, home = self._base(resource)
        if plan.resource_id != resource.id or Path(plan.target).resolve() != home or Path(plan.source).resolve() != repo or snapshot_token(repo) != plan.token:
            raise UserError("恢复目标或快照已改变，请重新预览。")
        self.verify(resource, context)
        self._check_previous_task(home)
        options = ["restore", "--node", "local", "--full", "--apply", "--home", str(home)]
        if resource.options.get("workspace"):
            options += ["--workspace", str(Path(resource.options["workspace"]).expanduser())]
        passphrase = resource.options.get("passphrase_file") or str(home / ".hermes-backup-passphrase")
        if not Path(passphrase).expanduser().is_file():
            raise UserError("请在资源设置中选择受保护的备份口令文件。口令不能写入聊天或命令参数值。")
        options += ["--passphrase-file", str(Path(passphrase).expanduser())]
        context.log("开始完整恢复，已禁止强制取消；等待工具完成并检查救援目录。")
        # Share the native backup lock with scheduled backup jobs. The CLI's
        # current restore path has no lock of its own; execute in this process.
        guarded = """import runpy,sys
from pathlib import Path
script=Path(sys.argv[1]); home=Path(sys.argv[2]); args=sys.argv[3:]
sys.path.insert(0,str(script.parent))
from hermes_backup.safety import BackupRunLock
sys.argv=[str(script),*args]
with BackupRunLock(home) as guard:
    namespace=runpy.run_path(str(script),run_name='agent_manager_native_restore')
    code=namespace['main']()
    if code:
        raise RuntimeError('restore failed')
    guard.finish(status='completed')
"""
        invocation = [command[0], "-c", guarded, command[1], str(home), *command[2:], *options]
        report = json_result(run_process(invocation, context, env=self._env(home), cancellable=False, timeout=1800))
        if report.get("applied") is not True:
            raise UserError("工具未确认恢复成功，请检查目标与救援资料。")
        return report
