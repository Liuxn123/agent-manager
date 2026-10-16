from __future__ import annotations

import os
import subprocess
import threading

from ..domain import Resource, UserError
from ..runtime import TaskContext
from .local import required_directory
from ..profiles import RECORD_NOTE, sources_for
from ..process_inventory import process_names


class AgentAdapter:
    capabilities = frozenset({"observe", "start", "stop", "backup", "verify", "restore", "open", "records"})
    PROCESS_NAMES = {
        "codex": {"codex", "codex.exe"},
        "workbuddy": {"workbuddy", "workbuddy.exe"},
        "codebuddy": {"codebuddy", "codebuddy.exe"},
        "claude code": {"claude", "claude.exe"},
    }
    COMMAND_MARKERS = {
        "codex": ("@openai/codex",),
        "claude code": ("@anthropic-ai/claude-code", "claude-code/cli"),
    }
    CLI_HOSTS = {"node", "node.exe", "nodejs", "bun", "bun.exe", "deno", "deno.exe"}

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
        return self.external_pids_many([resource])[resource.id]

    def external_pids_many(self, resources: list[Resource], cancel=None) -> dict[str, list[int]]:
        """Share one process inventory across all registrations; retain CLI detection."""
        import psutil
        from pathlib import Path
        rules = []
        matches = {resource.id: [] for resource in resources}
        for resource in resources:
            engine = str(resource.options.get("engine", "")).casefold()
            names = set(self.PROCESS_NAMES.get(engine, set()))
            if executable := resource.options.get("executable"):
                names.add(Path(executable).name.casefold())
            rules.append((resource.id, names, self.COMMAND_MARKERS.get(engine, ())))
        if not any(names or markers for _, names, markers in rules):
            return matches
        for pid, process_name in process_names():
            if cancel is not None and cancel.is_set():
                break
            try:
                name = str(process_name).casefold()
                executable_name = ""
                # Linux may truncate the comm name. Only inspect plausible matches.
                if any(candidate.startswith(name) and candidate != name for _, names, _ in rules for candidate in names if name):
                    executable_name = Path(psutil.Process(pid).exe()).name.casefold()
                command_line = []
                if name in self.CLI_HOSTS and any(markers for _, _, markers in rules):
                    command_line = [str(part).replace("\\", "/").casefold() for part in psutil.Process(pid).cmdline()]
                for identity, names, markers in rules:
                    if (name in names or executable_name in names or
                            any(marker in part for marker in markers for part in command_line)):
                        matches[identity].append(pid)
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
