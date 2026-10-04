from __future__ import annotations

import base64
import json
import re
import shlex
import os
import shutil
from pathlib import Path

from ..domain import Resource, RestorePlan, UserError
from ..runtime import TaskContext, json_result, run_process
from .server_worker import SERVER_PROGRAM

ERRORS = {"target_not_empty": "服务器目标目录非空，首版不会覆盖。请选择新目录。",
          "snapshot_changed": "服务器快照已变化，请重新预览。",
          "verification_failed": "服务器快照校验失败，已阻止恢复。",
          "passphrase_file_required": "请配置服务器上的受保护口令文件。",
          "unsafe_target": "恢复目标不安全或与现有目录重叠。",
          "wrong_account": "SSH 登录账号与服务账号不一致；请使用服务账号或具备 runuser 权限的管理员。"}


def ssh_command(resource: Resource, payload: dict) -> list[str]:
    options = resource.options
    host = str(options.get("host", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:@\[\]-]*", host):
        raise UserError("请设置有效 SSH 别名或 user@host，不能包含空格或选项。")
    try:
        port = int(options.get("port", 22))
    except (TypeError, ValueError) as exc:
        raise UserError("SSH 端口必须是数字。") from exc
    if not 1 <= port <= 65535:
        raise UserError("SSH 端口超出范围。")
    executable = options.get("ssh") or shutil.which("ssh")
    if not executable and os.name == "nt":
        candidate = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/OpenSSH/ssh.exe"
        executable = str(candidate) if candidate.is_file() else None
    command = [str(executable or "ssh"), "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-p", str(port)]
    if options.get("identity_file"):
        path = Path(options["identity_file"]).expanduser()
        if not path.is_file():
            raise UserError("SSH 私钥文件不存在。")
        command += ["-i", str(path)]
    if options.get("ssh_config"):
        path = Path(options["ssh_config"]).expanduser()
        if not path.is_file():
            raise UserError("SSH 配置文件不存在。")
        command += ["-F", str(path)]
    if user := str(options.get("user", "")).strip():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user):
            raise UserError("SSH 登录账号格式不正确。")
        command += ["-l", user]
    if options.get("known_hosts"):
        path = Path(options["known_hosts"]).expanduser()
        if not path.is_file():
            raise UserError("主机指纹文件不存在，请先核对服务器指纹。")
        command += ["-o", "UserKnownHostsFile=" + str(path)]
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    python = str(options.get("python", "python3")).strip()
    if not python or python.startswith("-") or "\n" in python:
        raise UserError("远端 Python 配置无效。")
    command += [host, shlex.join([python, "-u", "-c", SERVER_PROGRAM, encoded])]
    return command


def existing_connection() -> dict:
    """Reuse public connection parameters, never import passwords or key content."""
    mapping = {"host": "HOST", "user": "USER", "port": "PORT", "identity_file": "KEY_FILE",
               "ssh_config": "SSH_CONFIG", "known_hosts": "KNOWN_HOSTS", "python": "PYTHON",
               "home": "HERMES_HOME", "backup_repo": "BACKUP_REPO", "knowledge_repo": "KNOWLEDGE_REPO",
               "run_user": "BACKUP_USER"}
    values = {key: os.environ["HERMES_SERVER_" + suffix].strip() for key, suffix in mapping.items()
              if os.environ.get("HERMES_SERVER_" + suffix)}
    ssh_root = Path.home() / ".ssh"
    for key, name in [("ssh_config", "config"), ("known_hosts", "known_hosts")]:
        try:
            if key not in values and (ssh_root / name).is_file():
                values[key] = str(ssh_root / name)
        except OSError:
            continue
    return values


class ServerHermesAdapter:
    capabilities = frozenset({"observe", "backup", "verify", "restore", "start", "stop", "restart", "logs"})

    def _run(self, resource: Resource, action: str, context: TaskContext, **extra) -> dict:
        payload = {**resource.options, "action": action, **extra}
        command = ssh_command(resource, payload)
        report = json_result(run_process(command, context, timeout=1900 if action in {"backup", "restore"} else 600,
                                         cancellable=action in {"observe", "verify", "plan_restore", "logs"}))
        if report.get("ok") is not True:
            code = report.get("error", "unknown")
            raise UserError(ERRORS.get(code, f"服务器操作未完成（{code}）。请检查目录、工具依赖、运行账号和权限。"))
        return report["result"]

    def observe(self, resource: Resource, context: TaskContext) -> dict:
        return self._run(resource, "observe", context)

    def logs(self, resource: Resource, context: TaskContext) -> dict:
        from ..security import redact
        result = self._run(resource, "logs", context)
        return {"text": redact(str(result.get("text", ""))), "note": "最近 80 行网关日志，只读查看，不写入管家任务结果。"}

    def backup(self, resource: Resource, context: TaskContext) -> dict:
        return self._run(resource, "backup", context)

    def verify(self, resource: Resource, context: TaskContext) -> dict:
        return self._run(resource, "verify", context)

    def start(self, resource: Resource, context: TaskContext) -> dict:
        return self._run(resource, "start", context)

    def stop(self, resource: Resource, context: TaskContext) -> dict:
        return self._run(resource, "stop", context)

    def restart(self, resource: Resource, context: TaskContext) -> dict:
        return self._run(resource, "restart", context)

    def plan_restore(self, resource: Resource, context: TaskContext, target: str) -> RestorePlan:
        report = self._run(resource, "plan_restore", context, target=target)
        return RestorePlan(resource.id, report["plan_token"], resource.options["backup_repo"], target,
                           {**report["preview"], "target": target, "note": "恢复到服务器空目录。不会自动安装 runtime 或启动网关；现有服务器原地覆盖将在演练后扩展。"})

    def restore(self, resource: Resource, plan: RestorePlan, context: TaskContext) -> dict:
        if plan.resource_id != resource.id or plan.source != resource.options.get("backup_repo"):
            raise UserError("服务器配置已改变，请重新预览。")
        return self._run(resource, "restore", context, target=plan.target, plan_token=plan.token)
