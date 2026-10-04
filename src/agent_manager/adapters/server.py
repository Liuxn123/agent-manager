from __future__ import annotations

import base64
import json
import re
import shlex
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
    command = [str(options.get("ssh", "ssh")), "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-p", str(port)]
    if options.get("identity_file"):
        path = Path(options["identity_file"]).expanduser()
        if not path.is_file():
            raise UserError("SSH 私钥文件不存在。")
        command += ["-i", str(path)]
    if options.get("ssh_config"):
        command += ["-F", str(Path(options["ssh_config"]).expanduser())]
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    python = str(options.get("python", "python3")).strip()
    if not python or python.startswith("-") or "\n" in python:
        raise UserError("远端 Python 配置无效。")
    command += [host, shlex.join([python, "-u", "-c", SERVER_PROGRAM, encoded])]
    return command


class ServerHermesAdapter:
    capabilities = frozenset({"observe", "backup", "verify", "restore", "start", "stop", "restart"})

    def _run(self, resource: Resource, action: str, context: TaskContext, **extra) -> dict:
        payload = {**resource.options, "action": action, **extra}
        command = ssh_command(resource, payload)
        report = json_result(run_process(command, context, timeout=1900 if action in {"backup", "restore"} else 600,
                                         cancellable=action in {"observe", "verify", "plan_restore"}))
        if report.get("ok") is not True:
            code = report.get("error", "unknown")
            raise UserError(ERRORS.get(code, f"服务器操作未完成（{code}）。请检查目录、工具依赖、运行账号和权限。"))
        return report["result"]

    def observe(self, resource: Resource, context: TaskContext) -> dict:
        return self._run(resource, "observe", context)

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
