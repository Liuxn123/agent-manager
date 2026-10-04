from __future__ import annotations

import os
import shlex
import subprocess
import threading

from ..domain import Resource, UserError
from ..runtime import TaskContext
from .local import required_directory
from ..profiles import RECORD_NOTE, sources_for


class AgentAdapter:
    capabilities = frozenset({"observe", "start", "stop", "backup", "verify", "restore", "open", "records"})

    def __init__(self) -> None:
        self._processes: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def observe(self, resource: Resource, context: TaskContext) -> dict:
        components = sources_for(resource)
        from pathlib import Path
        with self._lock:
            process = self._processes.get(resource.id)
            running = bool(process and process.poll() is None)
        external = self.external_pids(resource)
        return {"engine": resource.options.get("engine", "通用 Agent"), "components": [
                {"label": item["label"], "path": item["path"], "exists": bool(item["path"] and Path(item["path"]).is_dir())} for item in components],
                "state": "本工具启动的进程正在运行" if running else "发现原应用正在运行" if external else "未发现已识别的进程",
                "external_process_count": len(external),
                "pid": process.pid if running else None,
                "note": RECORD_NOTE}

    def external_pids(self, resource: Resource) -> list[int]:
        import psutil
        from pathlib import Path
        names = {"Codex": {"codex", "codex.exe"}, "WorkBuddy": {"workbuddy", "workbuddy.exe"},
                 "CodeBuddy": {"codebuddy", "codebuddy.exe"}, "Claude Code": {"claude", "claude.exe"}}.get(resource.options.get("engine"), set())
        if executable := resource.options.get("executable"):
            names = names | {Path(executable).name.casefold()}
        matches = []
        for process in psutil.process_iter(["pid", "name"]):
            try:
                if str(process.info["name"]).casefold() in names:
                    matches.append(process.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return matches

    def is_running(self, resource: Resource) -> bool:
        with self._lock:
            process = self._processes.get(resource.id)
            return bool(process and process.poll() is None)

    def start(self, resource: Resource, context: TaskContext) -> dict:
        root = required_directory(resource.options, "path")
        executable = str(resource.options.get("executable", "")).strip()
        arguments = resource.options.get("arguments", [])
        if not executable or not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
            raise UserError("请设置可执行文件和参数 JSON 数组。")
        if executable.lower().endswith((".bat", ".cmd")):
            raise UserError("首版请选择实际可执行文件；Python 脚本用 Python 解释器配合脚本路径启动。")
        context.checkpoint()
        with self._lock:
            if self.is_running_unlocked(resource.id):
                raise UserError("此 Agent 已由本工具启动。")
            try:
                process = subprocess.Popen([executable, *arguments], cwd=root, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError as exc:
                raise UserError("无法启动 Agent，请检查可执行文件和参数。") from exc
            self._processes[resource.id] = process
        try:
            code = process.wait(timeout=0.5)
            return {"started": False, "exited": True, "exit_code": code, "note": "进程已退出；交互式 CLI 请通过终端运行。"}
        except subprocess.TimeoutExpired:
            return {"started": True, "pid": process.pid, "note": "管理非交互进程；输出由 Agent 自身日志管理，未采集凭据或会话。"}

    def is_running_unlocked(self, identity: str) -> bool:
        process = self._processes.get(identity)
        return bool(process and process.poll() is None)

    def stop(self, resource: Resource, context: TaskContext) -> dict:
        with self._lock:
            process = self._processes.get(resource.id)
            if not process or process.poll() is not None:
                raise UserError("没有本工具启动的运行进程。")
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise UserError("进程尚未退出，请通过 Agent 自身工具停止；本工具未强杀进程树。") from exc
        return {"stopped": True, "pid": process.pid}

    def running_ids(self) -> list[str]:
        with self._lock:
            return [identity for identity in self._processes if self.is_running_unlocked(identity)]
