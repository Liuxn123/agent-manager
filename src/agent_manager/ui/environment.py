"""Development environment inventory and user-managed local settings."""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QMessageBox, QPushButton, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .components import ReadableTable, card
from .tasks import ReadWorker
from .window import button


CONFIG_KEY = "development_environment"
HISTORY_LIMIT = 100
TOOLS = (
    ("Python", "python", ["--version"], "Python.Python.3.12"),
    ("Node.js", "node", ["--version"], "OpenJS.NodeJS.LTS"),
    ("Git", "git", ["--version"], "Git.Git"),
    ("JDK (Java)", "java", ["-version"], "EclipseAdoptium.Temurin.21.JDK"),
    ("Docker", "docker", ["--version"], "Docker.DockerDesktop"),
    ("WSL", "wsl", ["--status"], ""),
    ("VS Code", "code", ["--version"], "Microsoft.VisualStudioCode"),
    ("Cursor", "cursor", ["--version"], "Anysphere.Cursor"),
)
ENVIRONMENT_VARIABLES = ("PATH", "PYTHONPATH", "NODE_HOME", "JAVA_HOME", "WSL_DISTRO_NAME")


def _version(executable: str, arguments: list[str], cancel) -> str:
    try:
        result = subprocess.run(
            [executable, *arguments], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=3, check=False, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = (result.stdout or result.stderr).strip().splitlines()
        return output[0][:140] if output else ("可用" if result.returncode == 0 else "读取失败")
    except (OSError, subprocess.TimeoutExpired):
        return "版本读取失败"


def collect_environment(cancel):
    """Collect only local tool presence, version, executable path and allowlisted vars."""
    found = []
    for name, command, args, package_id in TOOLS:
        if cancel.is_set():
            return None
        executable = sys.executable if name == "Python" else shutil.which(command)
        if executable:
            version = platform.python_version() if name == "Python" and Path(sys.executable).resolve() == Path(executable).resolve() else _version(executable, args, cancel)
            found.append({"name": name, "command": command, "package_id": package_id,
                          "path": executable, "version": version, "installed": True})
        else:
            found.append({"name": name, "command": command, "package_id": package_id,
                          "path": "", "version": "未检测到", "installed": False})
    return {
        "tools": found,
        "winget": shutil.which("winget") or "",
        "os": f"{platform.system()} {platform.release()} · {platform.machine()}",
    }


class EnvironmentPage(QWidget):
    """Manage local development tool paths and inspect allowlisted environment vars."""

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.store = window.store
        self.worker = None
        self.loaded_once = False
        self.closed = False
        self.process: QProcess | None = None
        self.report = None
        saved = self.store.setting(CONFIG_KEY, {}) or {}
        if not isinstance(saved, dict):
            saved = {}
        self.paths = saved.get("paths", {})
        self.history = saved.get("history", [])
        self.setObjectName("EnvironmentPage")
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 20, 18)
        layout.setSpacing(11)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("电脑与环境")
        title.setObjectName("Title")
        subtitle = QLabel("集中管理开发软件、安装路径、环境变量和工具联动。")
        subtitle.setObjectName("Subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        self.refresh_button = button("刷新检测", self.refresh)
        header.addWidget(self.refresh_button)
        self.install_button = button("＋ 安装软件", self.install_selected, True)
        header.addWidget(self.install_button)
        layout.addLayout(header)

        metrics = QHBoxLayout()
        metrics.setSpacing(9)
        self.metrics = []
        for heading, note in (("已安装环境", "本机检测到的开发工具"), ("可用更新", "需手动触发 winget"),
                              ("已配置路径", "本地保存的路径项"), ("包管理器", "Windows Package Manager")):
            panel, value, detail = card(heading, "—", note)
            panel.setObjectName("EnvironmentMetric")
            value.setObjectName("EnvironmentMetricValue")
            detail.setObjectName("EnvironmentMetricNote")
            metrics.addWidget(panel, 1)
            self.metrics.append((value, detail))
        layout.addLayout(metrics)

        body = QGridLayout()
        body.setHorizontalSpacing(10)
        body.setVerticalSpacing(10)
        tools_card = self._card("开发环境安装", "仅显示本机检测结果；安装与更新需要你逐项确认。")
        tools_layout = tools_card.layout()
        self.system_note = QLabel("正在读取本机开发工具信息…")
        self.system_note.setObjectName("Subtitle")
        tools_layout.addWidget(self.system_note)
        self.tools_table = ReadableTable(0, 5)
        self.tools_table.setHorizontalHeaderLabels(["软件 / 环境", "当前版本", "安装状态", "可执行路径", "操作"])
        self.tools_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tools_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.tools_table.setSelectionBehavior(ReadableTable.SelectionBehavior.SelectRows)
        self.tools_table.setEditTriggers(ReadableTable.EditTrigger.NoEditTriggers)
        self.tools_table.verticalHeader().setDefaultSectionSize(42)
        self.tools_table.verticalHeader().hide()
        tools_layout.addWidget(self.tools_table, 1)
        body.addWidget(tools_card, 0, 0, 2, 1)

        path_card = self._card("安装路径与工作目录", "新安装会尝试使用所选目录；是否生效取决于软件安装器。不会移动已有文件。")
        path_layout = path_card.layout()
        self.path_fields: dict[str, object] = {}
        for key, label in (("root", "默认安装根目录"), ("python", "Python 安装目录"),
                           ("node", "Node.js 安装目录"), ("tools", "工具安装目录"), ("cache", "下载缓存目录")):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            field = QPushButton()
            field.setObjectName("PathValueButton")
            field.setText(str(self.paths.get(key, "未设置")))
            field.setToolTip(str(self.paths.get(key, "")))
            field.setProperty("pathKey", key)
            field.clicked.connect(lambda _=False, k=key: self.choose_path(k))
            row.addWidget(field, 1)
            row.addWidget(button("选择…", lambda _=False, k=key: self.choose_path(k)))
            path_layout.addLayout(row)
            self.path_fields[key] = field
        body.addWidget(path_card, 0, 1)

        env_card = self._card("环境变量", "仅列出常用开发变量。编辑写入当前 Windows 用户设置。")
        env_layout = env_card.layout()
        self.env_table = ReadableTable(0, 3)
        self.env_table.setHorizontalHeaderLabels(["变量名", "当前值", "操作"])
        self.env_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.env_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.env_table.verticalHeader().hide()
        env_layout.addWidget(self.env_table, 1)
        self.env_note = QLabel("未保存的进程变量不会自动写入系统。")
        self.env_note.setObjectName("Subtitle")
        env_layout.addWidget(self.env_note)
        body.addWidget(env_card, 1, 1)
        body.setColumnStretch(0, 7)
        body.setColumnStretch(1, 5)
        body.setRowStretch(0, 3)
        body.setRowStretch(1, 2)
        layout.addLayout(body, 1)

        history_card = self._card("安装记录 / 最近变更", "只记录在本页面发起的操作。")
        history_layout = history_card.layout()
        self.history_table = ReadableTable(0, 4)
        self.history_table.setHorizontalHeaderLabels(["时间", "操作", "目标", "结果"])
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history_table.verticalHeader().hide()
        self.history_table.setMaximumHeight(170)
        history_layout.addWidget(self.history_table)
        layout.addWidget(history_card)

    @staticmethod
    def _card(title: str, note: str) -> QFrame:
        panel = QFrame()
        panel.setObjectName("EnvironmentCard")
        box = QVBoxLayout(panel)
        box.setContentsMargins(14, 12, 14, 13)
        box.setSpacing(8)
        heading = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("SectionTitle")
        heading.addWidget(label, 1)
        box.addLayout(heading)
        subtitle = QLabel(note)
        subtitle.setObjectName("EnvironmentMetricNote")
        subtitle.setWordWrap(True)
        box.addWidget(subtitle)
        return panel

    def _save(self):
        self.store.set_setting(CONFIG_KEY, {"paths": self.paths, "history": self.history[-HISTORY_LIMIT:]})

    def _record(self, action: str, target: str, result: str):
        self.history.insert(0, {"time": datetime.now().astimezone().isoformat(timespec="seconds"),
                                "action": action, "target": target, "result": result})
        self.history = self.history[:HISTORY_LIMIT]
        self._save()
        self._fill_history()

    def choose_path(self, key: str):
        current = str(self.paths.get(key, Path.home()))
        path = QFileDialog.getExistingDirectory(self, "选择目录", current)
        if not path:
            return
        path = str(Path(path).resolve())
        self.paths[key] = path
        field = self.path_fields[key]
        field.setText(path)
        field.setToolTip(path)
        self._record("修改路径", key, path)

    def refresh(self):
        if self.closed or self.worker:
            return
        self.refresh_button.setEnabled(False)
        self.install_button.setEnabled(False)
        worker = ReadWorker(collect_environment)
        self.worker = worker

        def finished(report):
            self.worker = None
            if self.closed:
                return
            self.refresh_button.setEnabled(True)
            self.report = report
            if report is None:
                self.system_note.setText("检测失败，请重试。")
                return
            self.loaded_once = True
            self.system_note.setText(f"系统：{report['os']} · 信息仅来自本机安装路径与版本命令。")
            self.metrics[0][0].setText(str(sum(1 for row in report['tools'] if row['installed'])))
            self.metrics[0][1].setText("/ 8 项常用开发工具")
            self.metrics[1][0].setText("未检查")
            self.metrics[2][0].setText(str(sum(bool(value) for value in self.paths.values())))
            self.metrics[3][0].setText("可用" if report["winget"] else "未安装")
            self.metrics[3][1].setText(report["winget"] or "需安装 Windows App Installer")
            self.install_button.setEnabled(bool(report["winget"]))
            self._fill_tools()
            self._fill_environment()
            self._fill_history()

        worker.signals.finished.connect(finished)
        self.window.pool.start(worker)

    def _fill_tools(self):
        if not self.report:
            return
        self.tools_table.setRowCount(len(self.report["tools"]))
        for row, tool in enumerate(self.report["tools"]):
            state = "已安装" if tool["installed"] else "未安装"
            values = (tool["name"], tool["version"], state, tool["path"] or "—")
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 3:
                    item.setToolTip(value)
                self.tools_table.setItem(row, col, item)
            actions = QWidget()
            action_layout = QHBoxLayout(actions)
            action_layout.setContentsMargins(2, 1, 2, 1)
            if tool["installed"]:
                action_layout.addWidget(button("打开目录", lambda _=False, path=tool["path"]: self.open_target(path)))
                action_layout.addWidget(button("更新", lambda _=False, item=tool: self.run_package(item, "upgrade"), bool(self.report.get("winget") and tool["package_id"])))
            elif tool["name"] == "WSL" and sys.platform == "win32":
                action_layout.addWidget(button("安装", self.install_wsl))
            else:
                action_layout.addWidget(button("安装", lambda _=False, item=tool: self.run_package(item, "install"), bool(self.report.get("winget") and tool["package_id"])))
            self.tools_table.setCellWidget(row, 4, actions)
        self.tools_table.resizeRowsToContents()

    def open_target(self, path: str):
        target = Path(path)
        if target.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent if target.is_file() else target)))
        else:
            self.system_note.setText("可执行文件已不存在，请刷新检测。")

    def install_selected(self):
        if not self.report or not self.report.get("winget"):
            return
        row = self.tools_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "选择软件", "请先在列表中选中一个开发工具。")
            return
        self.run_package(self.report["tools"][row], "install")

    def run_package(self, tool: dict, action: str):
        if not self.report or not self.report.get("winget") or not tool.get("package_id"):
            return
        label = "安装" if action == "install" else "更新"
        answer = QMessageBox.question(self, f"确认{label}", f"将使用 winget {label}{tool['name']}（{tool['package_id']}）。\n\n安装器可能联网并修改本机软件。是否继续？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        args = ["--id", tool["package_id"], "--exact", "--source", "winget", "--accept-source-agreements", "--interactive"]
        if action == "install":
            preferred_path = self.paths.get({"Python": "python", "Node.js": "node"}.get(tool["name"], "tools")) or self.paths.get("root")
            if preferred_path:
                args.extend(["--location", str(preferred_path)])
        command = "install" if action == "install" else "upgrade"
        self.process = QProcess(self)
        self.process.setProgram(self.report["winget"])
        self.process.setArguments([command, *args])
        self.process.finished.connect(lambda code, status: self._package_finished(tool, action, code, status))
        self.process.errorOccurred.connect(lambda _error: self._package_error(tool, action))
        self.process.start()
        self._record(label, tool["name"], "已启动")
        self.system_note.setText(f"正在执行 {label}：{tool['name']}；请按安装器提示完成。")
        self._set_busy(True)

    def install_wsl(self):
        answer = QMessageBox.warning(
            self, "安装 WSL", "Windows 的 WSL 安装可能启用系统组件、下载 Ubuntu 并要求重启。\n"
            "此操作会启动系统安装命令，可能需要管理员权限。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.process = QProcess(self)
        self.process.setProgram(shutil.which("wsl") or "wsl.exe")
        self.process.setArguments(["--install"])
        self.process.finished.connect(lambda code, status: self._package_finished({"name": "WSL"}, "install", code, status))
        self.process.errorOccurred.connect(lambda _error: self._package_error({"name": "WSL"}, "install"))
        self.process.start()
        self._record("安装", "WSL", "已启动")
        self.system_note.setText("WSL 安装已启动；可能需要管理员权限并重启 Windows。")
        self._set_busy(True)

    def _package_error(self, tool, action):
        if self.process:
            self._record("安装" if action == "install" else "更新", tool["name"], "无法启动安装程序")
            self._set_busy(False)
            self.process = None
            self.system_note.setText("无法启动安装程序，请检查 winget / Windows 功能是否可用。")

    def _package_finished(self, tool, action, code, _status):
        result = "成功" if code == 0 else f"退出码 {code}"
        self._record("安装" if action == "install" else "更新", tool["name"], result)
        self._set_busy(False)
        self.process = None
        self.system_note.setText(f"{tool['name']} 操作完成（{result}）。")
        self.refresh()

    def _set_busy(self, busy: bool):
        self.refresh_button.setEnabled(not busy)
        self.install_button.setEnabled(not busy and bool(self.report and self.report.get("winget")))
        for row in range(self.tools_table.rowCount()):
            widget = self.tools_table.cellWidget(row, 4)
            if widget:
                for child in widget.findChildren(QPushButton):
                    child.setEnabled(not busy)

    def _fill_environment(self):
        self.env_table.setRowCount(len(ENVIRONMENT_VARIABLES))
        for row, name in enumerate(ENVIRONMENT_VARIABLES):
            value = __import__("os").environ.get(name, "")
            shown = value if name != "PATH" else f"{len(value.split(';' if sys.platform == 'win32' else ':')) if value else 0} 个路径项"
            self.env_table.setItem(row, 0, QTableWidgetItem(name))
            value_item = QTableWidgetItem(shown or "未设置")
            value_item.setToolTip(value)
            self.env_table.setItem(row, 1, value_item)
            edit = button("编辑", lambda _=False, key=name: self.edit_environment_variable(key), sys.platform == "win32")
            self.env_table.setCellWidget(row, 2, edit)
        if sys.platform != "win32":
            self.env_note.setText("当前系统仅展示进程环境变量；用户级持久化编辑仅支持 Windows。")

    def edit_environment_variable(self, name: str):
        if sys.platform != "win32":
            return
        import os
        current = os.environ.get(name, "")
        value, accepted = QInputDialog.getMultiLineText(self, f"编辑 {name}", "用户级环境变量值：", current)
        if not accepted:
            return
        if "\0" in value:
            QMessageBox.warning(self, "输入无效", "环境变量不能包含空字符。")
            return
        answer = QMessageBox.question(self, "确认保存", f"将 {name} 写入当前 Windows 用户的环境变量。\n是否保存此更改？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        import winreg
        key_path = r"Environment"
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                if value:
                    winreg.SetValueEx(key, name, 0, winreg.REG_EXPAND_SZ if "%" in value else winreg.REG_SZ, value)
                else:
                    try:
                        winreg.DeleteValue(key, name)
                    except FileNotFoundError:
                        pass
            os.environ[name] = value
            try:
                import ctypes
                ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 2, 5000, None)
            except (AttributeError, OSError):
                pass
            self._record("编辑环境变量", name, "已写入当前用户设置")
            self._fill_environment()
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", f"无法写入用户环境变量：{exc}")

    def _fill_history(self):
        self.history_table.setRowCount(len(self.history))
        for row, entry in enumerate(self.history):
            values = (entry.get("time", "").replace("T", " ")[:19], entry.get("action", ""),
                      entry.get("target", ""), entry.get("result", ""))
            for col, value in enumerate(values):
                self.history_table.setItem(row, col, QTableWidgetItem(value))

    def stop_updates(self):
        self.closed = True
        if self.worker:
            self.worker.cancel.set()
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            # Closing the app does not silently cancel a user-started installer.
            self.process.setParent(None)
