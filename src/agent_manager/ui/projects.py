from __future__ import annotations

import hashlib
import base64
import copy
import os
import re
import shutil
import subprocess
import json
import tempfile
from contextlib import nullcontext
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

from PySide6.QtCore import Qt, QUrl, QFileSystemWatcher, QTimer
from PySide6.QtGui import QDesktopServices, QTextOption
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QLabel, QPushButton,
    QComboBox, QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSplitter, QTextBrowser, QDialog, QDialogButtonBox, QFormLayout, QTextEdit, QCheckBox,
    QFileDialog, QMessageBox, QInputDialog, QListWidget, QListWidgetItem, QToolButton,
    QFrame, QSizePolicy, QScrollArea, QTreeWidget, QTreeWidgetItem, QApplication)

from ..domain import Resource, UserError
from ..project_workspaces import ProjectWorkspace, STATES, today
from ..project_prompts import project_prompt
from ..obsidian import project_uri, project_notes_uri, open_uri
from ..workbench import (TYPES, Catalog, work_root, project_context, summaries)
from ..task_migration import legacy_ref
from ..ai_task_execution import execute_codex_task
from .components import FlowLayout, nav_icon
from .tasks import ReadWorker
from .project_initialization import ProjectInitializationPanel
from .today_widgets import COLORS, ROW_DATA, label
from .project_widgets import (ProjectCardDelegate, ProjectMetric, ProjectMetrics, ProjectHero,
    ProjectOverview, overview_snapshot, milestone_rows, milestone_snapshot)
from .task_dialog import UnifiedTaskDialog


def _safe_git_remote_url(value):
    """Hide user info and query strings that may contain credentials."""
    value = str(value or "").strip()
    if "://" in value:
        try:
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            if parsed.port:
                host += f":{parsed.port}"
            return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        except ValueError:
            return "（地址格式无法安全显示）"
    if re.match(r"^[^\s:]+@[^\s:]+:.+$", value):
        return value.rsplit("@", 1)[-1]
    return value


def project_git_repositories(directory):
    """Find the project repository, including a single nested application repository."""
    try:
        root = Path(directory).resolve(strict=True)
    except OSError:
        return []
    if not root.is_dir():
        return []
    if (root / ".git").exists():
        return [root]

    ignored = {".agents", ".cache", ".codex", ".git", ".obsidian", ".venv", "build", "dist",
               "node_modules", "__pycache__", "reference", "references", "third-party", "third_party",
               "vendor", "vendors", "venv"}
    repositories = []
    for current, directories, files in os.walk(root):
        current_path = Path(current)
        if (current_path / ".git").exists():
            repositories.append(current_path)
            directories[:] = []
            continue
        depth = len(current_path.relative_to(root).parts)
        if depth >= 4:
            directories[:] = []
            continue
        directories[:] = [name for name in directories
                          if name.casefold() not in ignored and not name.startswith(".")
                          and not (current_path / name).is_symlink()
                          and not getattr(current_path / name, "is_junction", lambda: False)()]
        if ".git" in files:
            repositories.append(current_path)
            directories[:] = []
    return sorted(repositories, key=lambda path: path.as_posix().casefold())


def _run_git_read(git, root, args, environment):
    return subprocess.run(
        [git, "-c", "core.fsmonitor=false", "-C", str(root), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=6, check=False, shell=False, env=environment,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def read_project_git_status(directory, cancel, registered_repository=""):
    git = shutil.which("git")
    try:
        root = Path(directory).resolve(strict=True)
    except OSError:
        return {"ok": False, "text": "项目目录无法读取。"}
    if not git or not (root / ".git").exists():
        return {"ok": False, "text": "此项目没有可用的 Git 仓库。"}
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"}
    try:
        status = _run_git_read(git, root, ["status", "--porcelain=v1", "--branch", "--ahead-behind", "--untracked-files=all"], environment)
        if cancel.is_set():
            return None
        remotes = _run_git_read(git, root, ["remote", "-v"], environment)
        if cancel.is_set():
            return None
        last_commit = _run_git_read(git, root, ["log", "-1", "--format=%h%x09%s%x09%cr"], environment)
    except Exception:
        return {"ok": False, "text": "Git 状态读取失败或超时。请确认项目目录可访问、Git 已正确安装；没有修改仓库。"}
    if cancel.is_set():
        return None
    if status.returncode:
        return {"ok": False, "text": "Git 无法读取此目录状态；没有修改仓库。"}

    lines = status.stdout.splitlines()
    header = next((line[3:] for line in lines if line.startswith("## ")), "")
    branch_part, separator, tracking_part = header.partition("...")
    if branch_part.startswith("No commits yet on "):
        branch = branch_part.removeprefix("No commits yet on ")
    else:
        branch = branch_part or "HEAD（游离状态）"
    tracking = tracking_part.split(maxsplit=1)[0] if separator and tracking_part else ""
    ahead_match = re.search(r"\bahead (\d+)\b", tracking_part)
    behind_match = re.search(r"\bbehind (\d+)\b", tracking_part)
    ahead = int(ahead_match.group(1)) if ahead_match else 0
    behind = int(behind_match.group(1)) if behind_match else 0

    changed_files = []
    file_rows = []
    staged = unstaged = untracked = 0
    for line in lines:
        if not line or line.startswith("## "):
            continue
        code, path = line[:2], line[3:] if len(line) > 3 else ""
        if code == "??":
            untracked += 1
            state = "未跟踪"
        else:
            if code[0] not in {".", " "}:
                staged += 1
            if code[1] not in {".", " "}:
                unstaged += 1
            state_names = {"A": "新增", "M": "已修改", "D": "已删除", "R": "已重命名", "C": "已复制", "U": "冲突"}
            state = " / ".join(dict.fromkeys(state_names.get(char, "已修改") for char in code if char not in {".", " "}))
        changed_files.append(f"{code}  {path}  ·  {state}")
        file_rows.append({"code": code, "path": path, "state": state,
                          "staged": code != "??" and code[0] not in {".", " "},
                          "unstaged": code == "??" or code[1] not in {".", " "}})

    remote_rows = []
    if remotes.returncode == 0:
        for line in remotes.stdout.splitlines():
            if "\t" not in line:
                continue
            name, address = line.split("\t", 1)
            if " " not in address:
                continue
            url, kind = address.rsplit(" ", 1)
            direction = kind.strip("()")
            remote_rows.append((name, direction, _safe_git_remote_url(url)))

    change_count = len(changed_files)
    change_summary = ("工作区干净" if not change_count else
                      f"{change_count} 个变更文件（暂存 {staged}、未暂存 {unstaged}、未跟踪 {untracked}）")
    summary_parts = [f"分支：{branch}", change_summary]
    if tracking:
        summary_parts.append(f"上游：{tracking}（本地领先 {ahead}、落后 {behind}）")
    else:
        summary_parts.append("未设置上游分支")

    details = ["本地仓库", f"目录：{root}", f"当前分支：{branch}",
               f"上游分支：{tracking or '未设置'}"]
    if tracking:
        details.append(f"本地与上游差异：领先 {ahead} 个提交，落后 {behind} 个提交")
    else:
        details.append("本地与远程差异：当前分支尚未配置上游分支")
    details.extend((f"工作区变更：{change_count} 个文件", f"已暂存：{staged} 个",
                    f"未暂存：{unstaged} 个", f"未跟踪：{untracked} 个"))
    if last_commit.returncode == 0 and last_commit.stdout.strip():
        commit_hash, _, commit_info = last_commit.stdout.strip().partition("\t")
        subject, _, relative_time = commit_info.partition("\t")
        details.extend(("", "最近提交", f"{commit_hash}  {subject}  ({relative_time})"))
    else:
        details.extend(("", "最近提交", "仓库还没有提交记录。"))

    details.extend(("", "工作区文件"))
    if changed_files:
        details.extend(changed_files[:100])
        if len(changed_files) > 100:
            details.append(f"…另有 {len(changed_files) - 100} 个文件未显示。")
    else:
        details.append("没有本地变更。")

    details.extend(("", "远程仓库（只读）"))
    if remote_rows:
        for name, direction, address in remote_rows:
            details.append(f"{name}（{'获取' if direction == 'fetch' else '推送'}）：{address}")
    else:
        details.append("Git 配置中尚未登记远程仓库。")
    registered_repository = _safe_git_remote_url(registered_repository)
    if registered_repository:
        details.extend(("", f"项目信息登记的仓库：{registered_repository}"))
    details.extend(("", "远程领先/落后数量基于本地已有的远程跟踪记录；本次没有联网 fetch，也没有修改仓库。"))
    commit = None
    if last_commit.returncode == 0 and last_commit.stdout.strip():
        commit_hash, _, commit_info = last_commit.stdout.strip().partition("\t")
        subject, _, relative_time = commit_info.partition("\t")
        commit = {"hash": commit_hash, "subject": subject, "time": relative_time}
    return {"ok": True, "summary": "  ·  ".join(summary_parts), "text": "\n".join(details),
            "directory": str(root), "branch": branch, "tracking": tracking, "ahead": ahead, "behind": behind,
            "staged": staged, "unstaged": unstaged, "untracked": untracked, "change_count": change_count,
            "files": file_rows, "commit": commit, "remotes": remote_rows,
            "registered_repository": registered_repository}


def run_project_git_action(directory, action, cancel, commit_message="", token="", registered_repository="", file_path=""):
    allowed = {"init", "stage", "stage_file", "unstage_file", "unstage_all", "commit", "fetch", "pull", "push"}
    if action not in allowed:
        return {"ok": False, "message": "不支持此 Git 操作。"}
    try:
        root = Path(directory).resolve(strict=True)
    except OSError:
        return {"ok": False, "message": "项目仓库目录无法读取。"}
    if not root.is_dir():
        return {"ok": False, "message": "项目仓库路径不是目录。"}
    if action != "init" and not (root / ".git").exists():
        return {"ok": False, "message": "所选目录尚未初始化 Git。"}
    git = shutil.which("git")
    if not git:
        return {"ok": False, "message": "没有找到 Git，请先安装 Git。"}

    commands = {
        "init": ["init"],
        "stage": ["add", "-A"],
        "stage_file": ["add", "-A", "--", file_path],
        "unstage_file": ["restore", "--staged", "--", file_path],
        "unstage_all": ["reset", "--mixed", "HEAD"],
        "commit": ["commit", "-m", commit_message.strip()],
        "fetch": ["fetch"],
        "pull": ["pull", "--ff-only"],
        "push": ["push"],
    }
    command = commands[action]
    if action in {"stage_file", "unstage_file"} and (not file_path or Path(file_path).is_absolute() or
            ".." in Path(file_path).parts):
        return {"ok": False, "message": "文件路径无效，未执行 Git 操作。"}
    if action == "commit" and not commit_message.strip():
        return {"ok": False, "message": "请填写提交说明。"}
    if action == "init" and registered_repository:
        valid_remote = registered_repository.strip()
        if any(character.isspace() for character in valid_remote) or valid_remote.startswith("-"):
            return {"ok": False, "message": "项目登记的仓库地址格式不正确，请先在项目信息中修正。"}
        if "://" in valid_remote:
            parsed_remote = urlsplit(valid_remote)
            if (parsed_remote.scheme not in {"https", "http", "ssh", "git"} or not parsed_remote.netloc or
                    parsed_remote.password or parsed_remote.query or
                    parsed_remote.scheme in {"https", "http"} and parsed_remote.username):
                return {"ok": False, "message": "仓库地址不能包含账号、密码或查询参数，请在项目信息中填写干净的 HTTPS/SSH 地址。"}
    if action in {"fetch", "pull", "push"}:
        remotes = _run_git_read(git, root, ["remote"], {**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        if remotes.returncode or not remotes.stdout.strip():
            return {"ok": False, "message": "尚未配置远程仓库。请先在项目资料中设置仓库地址，或在 Git 中添加 origin。"}
        status = _run_git_read(git, root, ["status", "--porcelain=v1", "--branch"],
                               {**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        header = next((line[3:] for line in status.stdout.splitlines() if line.startswith("## ")), "")
        if "..." not in header and "origin" in remotes.stdout.splitlines():
            branch = _run_git_read(git, root, ["branch", "--show-current"],
                                   {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout.strip()
            if branch:
                if action == "push":
                    command = ["push", "-u", "origin", branch]
                elif action == "pull":
                    command = ["pull", "--ff-only", "origin", branch]

    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"}
    auth_scope = tempfile.TemporaryDirectory(prefix="agent-manager-git-") if token and action in {"fetch", "pull", "push"} else nullcontext("")
    try:
        with auth_scope as temporary:
            if temporary:
                askpass = Path(temporary) / "git-askpass.cmd"
                askpass.write_text(
                    "@echo off\r\npowershell.exe -NoProfile -NonInteractive -Command "
                    "\"[Console]::Out.Write([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:AGENT_MANAGER_GIT_TOKEN_B64)))\"\r\n",
                    encoding="utf-8",
                )
                environment["GIT_ASKPASS"] = str(askpass)
                environment["AGENT_MANAGER_GIT_TOKEN_B64"] = base64.b64encode(token.encode("utf-8")).decode("ascii")
            result = subprocess.run(
                [git, "-c", "core.fsmonitor=false", "-C", str(root), *command],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=120, check=False, shell=False, env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "Git 操作超时；请检查网络或仓库状态。"}
    except OSError:
        return {"ok": False, "message": "无法启动 Git 操作；请检查 Git 安装与目录权限。"}
    except Exception:
        return {"ok": False, "message": "Git 操作未完成；请检查项目目录、Git 配置和网络。"}
    if cancel.is_set():
        return None
    if result.returncode:
        from ..security import redact
        detail = redact((result.stderr or result.stdout).strip())
        return {"ok": False, "message": f"Git {action} 未完成。" + ("\n" + detail if detail else "")}

    if action == "init" and registered_repository:
        remote_check = _run_git_read(git, root, ["remote", "get-url", "origin"],
                                     {**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        if remote_check.returncode:
            remote_add = subprocess.run(
                [git, "-C", str(root), "remote", "add", "origin", registered_repository.strip()],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                check=False, shell=False, env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if remote_add.returncode:
                from ..security import redact
                return {"ok": False, "message": "仓库已初始化，但未能添加 origin。\n" +
                        redact((remote_add.stderr or remote_add.stdout).strip())}

    labels = {"init": "已初始化 Git 仓库", "stage": "已暂存全部变更", "stage_file": "文件已暂存",
              "unstage_file": "已取消暂存", "unstage_all": "已取消全部暂存", "commit": "提交已创建",
              "fetch": "已获取远程状态", "pull": "已完成快进拉取", "push": "已推送到远程仓库"}
    status = read_project_git_status(root, cancel, registered_repository)
    return {"ok": True, "message": labels[action], "status": status}


class ProjectGitStatusDialog(QDialog):
    def __init__(self, parent, project_name, repository, report, action_callback):
        super().__init__(parent)
        self.setWindowTitle(project_name + " · Git 状态")
        self.resize(900, 680)
        self.setMinimumSize(760, 540)
        self.repository = repository
        self.action_callback = action_callback
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        self.branch_label = QLabel()
        self.branch_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        heading.addWidget(self.branch_label)
        heading.addStretch(1)
        self.sync_label = QLabel()
        self.sync_label.setObjectName("Subtitle")
        heading.addWidget(self.sync_label)
        layout.addLayout(heading)

        metrics = QHBoxLayout()
        self.metric_labels = {}
        for key, title in (("change_count", "变更文件"), ("staged", "已暂存"),
                           ("unstaged", "未暂存"), ("untracked", "未跟踪")):
            card = QFrame()
            card.setFrameShape(QFrame.Shape.StyledPanel)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 8, 12, 8)
            value = QLabel("0")
            value.setStyleSheet("font-size: 20px; font-weight: 600;")
            caption = QLabel(title)
            caption.setObjectName("Subtitle")
            card_layout.addWidget(value)
            card_layout.addWidget(caption)
            metrics.addWidget(card, 1)
            self.metric_labels[key] = value
        layout.addLayout(metrics)

        self.tabs = QTabWidget()
        self.files_tree = QTreeWidget()
        self.files_tree.setColumnCount(3)
        self.files_tree.setHeaderLabels(["文件", "状态", "操作"])
        self.files_tree.setRootIsDecorated(True)
        self.files_tree.setAlternatingRowColors(True)
        self.files_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.files_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.files_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.files_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.empty_files = QLabel("工作区干净，没有本地变更。")
        self.empty_files.setAlignment(Qt.AlignmentFlag.AlignCenter)
        files_page = QWidget()
        files_layout = QVBoxLayout(files_page)
        files_layout.setContentsMargins(4, 6, 4, 4)
        files_layout.addWidget(self.empty_files)
        files_layout.addWidget(self.files_tree, 1)
        self.tabs.addTab(files_page, "工作区变更")

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setAcceptRichText(False)
        self.tabs.addTab(self.details, "仓库信息")
        layout.addWidget(self.tabs, 1)
        self.action_status = QLabel("")
        self.action_status.setWordWrap(True)
        self.action_status.setObjectName("Subtitle")
        layout.addWidget(self.action_status)

        commit_row = QHBoxLayout()
        self.commit_input = QLineEdit()
        self.commit_input.setPlaceholderText("提交说明（填写后提交已暂存的更改）")
        self.commit_input.textChanged.connect(
            lambda value: self.commit_button.setEnabled(bool(value.strip()) and getattr(self, "_has_staged", False)))
        self.commit_input.returnPressed.connect(self.commit_staged)
        commit_row.addWidget(self.commit_input, 1)
        self.commit_button = QPushButton("提交")
        self.commit_button.clicked.connect(self.commit_staged)
        commit_row.addWidget(self.commit_button)
        layout.addLayout(commit_row)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("刷新状态")
        self.refresh_button.clicked.connect(lambda: self.action_callback("refresh", ""))
        buttons.addWidget(self.refresh_button)
        self.stage_button = QPushButton("暂存全部变更")
        self.stage_button.clicked.connect(self.stage_all)
        buttons.addWidget(self.stage_button)
        self.unstage_button = QPushButton("取消全部暂存")
        self.unstage_button.clicked.connect(lambda: self.action_callback("unstage_all", ""))
        buttons.addWidget(self.unstage_button)
        self.fetch_button = QPushButton("获取")
        self.fetch_button.clicked.connect(lambda: self.action_callback("fetch", ""))
        buttons.addWidget(self.fetch_button)
        self.pull_button = QPushButton("↓ 拉取")
        self.pull_button.clicked.connect(lambda: self.action_callback("pull", ""))
        buttons.addWidget(self.pull_button)
        self.push_button = QPushButton("↑ 推送")
        self.push_button.clicked.connect(lambda: self.action_callback("push", ""))
        buttons.addWidget(self.push_button)
        buttons.addStretch(1)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.set_report(report)

    def set_busy(self, busy, message=""):
        for control in (self.refresh_button, self.stage_button, self.unstage_button, self.commit_button,
                        self.commit_input, self.fetch_button, self.pull_button, self.push_button, self.close_button):
            control.setEnabled(not busy)
        self.files_tree.setEnabled(not busy)
        self.action_status.setText(message or ("Git 操作进行中…" if busy else ""))

    def set_report(self, report, message=""):
        if not report.get("ok"):
            self.action_status.setText(report.get("text", "Git 状态读取失败。"))
            return
        self.branch_label.setText(f"分支  {report.get('branch') or 'HEAD（游离状态）'}")
        tracking = report.get("tracking")
        self.sync_label.setText(f"上游 {tracking}  ·  ↑ {report.get('ahead', 0)}  ↓ {report.get('behind', 0)}"
                                if tracking else "未设置上游分支")
        for key, value in self.metric_labels.items():
            value.setText(str(report.get(key, 0)))
        files = report.get("files", [])
        staged_files = [item for item in files if item.get("staged")]
        unstaged_files = [item for item in files if item.get("unstaged")]
        self._has_staged = bool(staged_files)
        self.stage_button.setEnabled(bool(unstaged_files))
        self.unstage_button.setEnabled(bool(staged_files))
        self.commit_button.setEnabled(bool(staged_files) and bool(self.commit_input.text().strip()))
        has_remote = bool(report.get("remotes"))
        self.fetch_button.setEnabled(has_remote)
        self.pull_button.setText(f"↓ 拉取 {report.get('behind', 0)}")
        self.push_button.setText(f"↑ 推送 {report.get('ahead', 0)}")
        self.pull_button.setEnabled(has_remote and bool(report.get("tracking")) and report.get("behind", 0) > 0)
        self.push_button.setEnabled(has_remote and (report.get("ahead", 0) > 0 or not report.get("tracking")))
        self.empty_files.setVisible(not files)
        self.files_tree.setVisible(bool(files))
        self.files_tree.clear()
        staged_group = QTreeWidgetItem([f"已暂存的更改 ({len(staged_files)})", "", ""])
        unstaged_group = QTreeWidgetItem([f"更改 ({len(unstaged_files)})", "", ""])
        self.files_tree.addTopLevelItems([staged_group, unstaged_group])
        for group, group_files, action, label_text in (
                (staged_group, staged_files, "unstage_file", "取消暂存"),
                (unstaged_group, unstaged_files, "stage_file", "暂存")):
            for file in group_files:
                child = QTreeWidgetItem([file.get("path", ""), file.get("state", ""), ""])
                child.setToolTip(0, file.get("path", ""))
                group.addChild(child)
                button = QToolButton()
                button.setText(label_text)
                button.clicked.connect(lambda checked=False, path=file.get("path", ""), op=action:
                                       self.action_callback(op, "", path))
                self.files_tree.setItemWidget(child, 2, button)
            group.setExpanded(True)
        staged_group.setHidden(not staged_files)
        unstaged_group.setHidden(not unstaged_files)

        commit = report.get("commit")
        info = [f"仓库目录：{report.get('directory', self.repository)}",
                f"当前分支：{report.get('branch') or 'HEAD（游离状态）'}",
                f"上游分支：{tracking or '未设置'}",
                f"同步情况：领先 {report.get('ahead', 0)} 个提交，落后 {report.get('behind', 0)} 个提交"
                if tracking else "同步情况：尚未设置上游分支",
                "", "最近提交："]
        info.append(f"{commit['hash']}  {commit['subject']}（{commit['time']}）" if commit else "仓库还没有提交记录。")
        info.extend(("", "远程仓库："))
        remotes = report.get("remotes", [])
        info.extend(f"{name}（{'获取' if direction == 'fetch' else '推送'}）：{address}"
                    for name, direction, address in remotes)
        if not remotes:
            info.append("尚未登记远程仓库。")
        if report.get("registered_repository"):
            info.extend(("", f"项目登记地址：{report['registered_repository']}"))
        info.extend(("", "领先/落后数量来自本地远程跟踪记录；刷新状态不会联网 fetch。"))
        self.details.setPlainText("\n".join(info))
        self.action_status.setText(message or report.get("summary", "状态已刷新。"))
        self.commit_button.setEnabled(bool(staged_files) and bool(self.commit_input.text().strip()))

    def stage_all(self):
        self.action_callback("stage", "")

    def commit_staged(self):
        message = self.commit_input.text().strip()
        if message and self.commit_button.isEnabled():
            self.action_callback("commit", message)

    def confirm_remote(self, action):
        label = "拉取远程更新（仅允许快进，不会自动创建合并提交）" if action == "pull" else "推送本地提交"
        if QMessageBox.question(self, "Git 远程操作", f"确认{label}？") == QMessageBox.StandardButton.Yes:
            self.action_callback(action, "")


class ProjectLogView(QWidget):
    """Present dated entries while keeping HANDOFF as the single source."""
    def __init__(self, view):
        super().__init__()
        self.view, self.text, self.entries = view, "", []
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.selector = QComboBox()
        self.selector.currentIndexChanged.connect(self.render)
        row.addWidget(self.selector, 1)
        self.all = QCheckBox("查看完整交接原文")
        self.all.toggled.connect(self.render)
        row.addWidget(self.all)
        layout.addLayout(row)
        layout.addWidget(view, 1)

    def set_text(self, text):
        self.text = text
        headings = list(re.finditer(r"(?m)^## (.+)$", text))
        self.entries = [(match.group(1), text[match.start():headings[i + 1].start() if i + 1 < len(headings) else len(text)].strip())
                        for i, match in enumerate(headings) if re.search(r"\d{4}-\d{2}-\d{2}", match.group(1))]
        self.entries.reverse()
        self.selector.blockSignals(True)
        self.selector.clear()
        for title, _ in self.entries:
            self.selector.addItem(title)
        self.selector.blockSignals(False)
        self.render()

    def render(self, *_):
        row = self.selector.currentIndex()
        if self.all.isChecked():
            self.view.setMarkdown(self.text)
        elif 0 <= row < len(self.entries):
            self.view.setMarkdown(self.entries[row][1])
        else:
            self.view.setPlainText("还没有按日期记录的日志。可在 Obsidian 中打开 HANDOFF.md，直接编写日志与交接内容。")


class ProjectPromptDialog(QDialog):
    def __init__(self, parent, item, *, initial=False):
        super().__init__(parent)
        self.setWindowTitle("项目 AI 提示词")
        self.resize(720, 600)
        layout = QVBoxLayout(self)
        hint = QLabel("选择用途，复制后交给 AI，并附上本轮具体需求。每次实质任务须更新状态、任务和项目动态。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.mode = QComboBox()
        self.mode.addItems(["新项目初始化", "已有项目接手"])
        self.mode.setCurrentIndex(0 if initial else 1)
        layout.addWidget(self.mode)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        self.copy_button = buttons.addButton("复制提示词", QDialogButtonBox.ButtonRole.ActionRole)
        self.copy_button.setProperty("primary", True)
        self.copy_button.clicked.connect(self.copy_prompt)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.item = item
        self.mode.currentIndexChanged.connect(self.refresh_prompt)
        self.refresh_prompt()

    def refresh_prompt(self, *_):
        self.preview.setPlainText(project_prompt(
            self.item["name"], self.item["id"], self.item["directory"], initial=self.mode.currentIndex() == 0))
        self.copy_button.setText("复制提示词")

    def copy_prompt(self):
        QApplication.clipboard().setText(self.preview.toPlainText())
        self.copy_button.setText("已复制")


class ProjectDialog(QDialog):
    def __init__(self, parent, legacy=None):
        super().__init__(parent)
        self.setWindowTitle("新建项目")
        self.resize(820, 680)
        layout = QVBoxLayout(self)
        note = QLabel("填写基本信息，按需调整规范、添加技能与资料，最后确认创建。")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("例如：个人工作台")
        self.folder_name = QLineEdit()
        self.folder_name.setPlaceholderText("例如：personal-workbench")
        self.translate_button = QPushButton("翻译为英文")
        self.translate_button.setToolTip("点击后会将项目名称发送至 Google 翻译；也可以直接手动填写英文名。")
        english_row = QHBoxLayout()
        english_row.addWidget(self.folder_name, 1)
        english_row.addWidget(self.translate_button)
        self.folder_hint = QLabel("将自动整理为仅含 a-z、0-9 和连字符的目录名。翻译仅在点击时进行。")
        self.folder_hint.setWordWrap(True)
        self.translation_worker = None
        self.goal = QTextEdit()
        self.goal.setPlaceholderText("这个项目希望完成什么？")
        self.entry = QCheckBox("在工作区的 Obsidian 建立同一份资料入口")
        self.entry.setChecked(True)
        if legacy:
            self.setWindowTitle("从旧归档继续 · 新建编号项目")
            note.setText("原归档保留原位。为接下来的工作分配新编号，创建状态、任务和日志，并引用原资料；不会搬动或复制业务文件。")
            self.name.setText(legacy["name"] + "-接续")
            self.folder_name.setText(re.sub(r"[^a-z0-9]+", "-", legacy["name"].lower()).strip("-") + "-continuation")
            self.goal.setPlainText("接续旧项目 " + legacy["name"] + "（" + legacy["id"] + "）。\n原归档资料：" + legacy["directory"] + "\n下一步：核对原项目状态、业务代码位置和本次目标。")
        form.addRow("项目名称", self.name)
        form.addRow("英文目录名", english_row)
        form.addRow("", self.folder_hint)
        form.addRow("项目目标", self.goal)
        form.addRow("", self.entry)
        basic_page = QWidget()
        basic_layout = QVBoxLayout(basic_page)
        basic_layout.addLayout(form)
        self.goal.setMaximumHeight(160)
        basic_layout.addStretch()
        self.initialization_panel = ProjectInitializationPanel(
            self, getattr(parent, "window", None), self.initialization_preview_context)
        self.initialization_panel.insertTab(0, basic_page, "项目信息")
        for i in range(self.initialization_panel.count()):
            self.initialization_panel.setTabText(i, f"{i + 1}. " + self.initialization_panel.tabText(i))
        self.initialization_panel.setCurrentIndex(0)
        layout.addWidget(self.initialization_panel, 1)
        self.initialization_selection = None
        self.finished.connect(self.initialization_panel.cancel_reads)
        footer = QHBoxLayout()
        self.step_label = QLabel()
        footer.addWidget(self.step_label, 1)
        self.back_button = QPushButton("上一步")
        self.back_button.clicked.connect(lambda: self.initialization_panel.setCurrentIndex(self.initialization_panel.currentIndex() - 1))
        footer.addWidget(self.back_button)
        self.next_button = QPushButton("下一步")
        self.next_button.setProperty("primary", True)
        self.next_button.clicked.connect(self.next_step)
        footer.addWidget(self.next_button)
        self.create_button = QPushButton("创建项目")
        self.create_button.setProperty("primary", True)
        self.create_button.clicked.connect(self.confirm)
        footer.addWidget(self.create_button)
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        footer.addWidget(cancel_button)
        self.translate_button.clicked.connect(self.translate_name)
        self.initialization_panel.currentChanged.connect(self.update_step)
        layout.addLayout(footer)
        self.update_step()

    def update_step(self, *_):
        index = self.initialization_panel.currentIndex()
        last = index == self.initialization_panel.count() - 1
        self.step_label.setText(f"第 {index + 1} 步，共 4 步")
        self.back_button.setEnabled(index > 0)
        self.next_button.setVisible(not last)
        self.next_button.setDefault(not last)
        self.create_button.setVisible(last)
        self.create_button.setDefault(last)

    def next_step(self):
        if self.initialization_panel.currentIndex() == 0 and not self.validate_basics():
            return
        try:
            self.initialization_panel.selection()
        except UserError as exc:
            QMessageBox.warning(self, "请检查所选内容", str(exc))
            return
        self.initialization_panel.setCurrentIndex(min(self.initialization_panel.currentIndex() + 1, 3))

    @staticmethod
    def _translate_name(cancel, name):
        if cancel.is_set():
            return None
        query = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": name})
        request = urllib.request.Request(
            "https://translate.googleapis.com/translate_a/single?" + query,
            headers={"User-Agent": "AgentManager/0.8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.loads(response.read(256_000).decode("utf-8"))
            result = "".join(part[0] for part in payload[0] if part and isinstance(part[0], str)).strip()
            return result or None
        except Exception:
            return None

    def translate_name(self):
        name = self.name.text().strip()
        if not name:
            QMessageBox.information(self, "填写项目名称", "先填写项目中文名称，再点击翻译。")
            return
        self.translate_button.setEnabled(False)
        self.translate_button.setText("翻译中…")
        worker = ReadWorker(lambda cancel: self._translate_name(cancel, name))
        self.translation_worker = worker
        worker.signals.finished.connect(self._translation_finished)
        self.parent().window.pool.start(worker)

    def _translation_finished(self, translated):
        self.translate_button.setEnabled(True)
        self.translate_button.setText("翻译为英文")
        self.translation_worker = None
        if not translated:
            QMessageBox.warning(self, "翻译未完成", "在线翻译暂时不可用。你可以手动填写英文目录名后继续。")
            return
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", translated).strip("-").lower()[:60].strip("-")
        self.folder_name.setText(slug)
        self.folder_hint.setText("翻译建议：" + translated + "。保存时会规范化为安全目录名：" + slug)

    def validate_basics(self):
        if not self.name.text().strip() or not self.folder_name.text().strip() or not self.goal.toPlainText().strip():
            QMessageBox.information(self, "补充项目信息", "请填写项目名称、英文目录名和项目目标。")
            return False
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", self.folder_name.text()).strip("-").lower()
        if not slug or len(slug) > 60:
            QMessageBox.information(self, "英文目录名无效", "目录名需为 1–60 个英文或数字字符；空格和标点会转换为连字符。")
            return False
        self.folder_name.setText(slug)
        return True

    def confirm(self):
        if self.initialization_panel.currentIndex() != 3:
            self.next_step()
            return
        if not self.validate_basics():
            self.initialization_panel.setCurrentIndex(0)
            return
        try:
            self.initialization_selection = self.initialization_panel.selection()
        except UserError as exc:
            QMessageBox.warning(self, "初始化选项需要检查", str(exc))
            return
        self.accept()

    def initialization_preview_context(self):
        project_root = self.parent().root() if self.parent() is not None else Path.cwd()
        folder = self.folder_name.text().strip() or "project-folder"
        identity = "P-" + today()[:4] + "-待分配"
        return Path(project_root) / "projects" / (identity + "-" + folder), {
            "NAME": self.name.text().strip() or "项目名称", "GOAL": self.goal.toPlainText().strip() or "待补充",
            "ID": identity, "DATE": today()}


class ProjectInfoDialog(QDialog):
    """Edit optional project metadata without changing the Markdown source files."""
    def __init__(self, parent, info=None, description=""):
        super().__init__(parent)
        info = info if isinstance(info, dict) else {}
        self.setWindowTitle("编辑项目信息")
        self.resize(680, 680)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("在这里统一编辑项目介绍、封面和登记信息。内容保存在本机项目登记中。"))
        layout.addWidget(QLabel("项目介绍"))
        self.description = QTextEdit(self)
        self.description.setPlaceholderText("写下项目背景、目标、范围或其他希望长期展示的说明…")
        self.description.setPlainText(description)
        self.description.setMaximumHeight(120)
        self.description.textChanged.connect(self.update_description_length)
        layout.addWidget(self.description)
        self.description_length = QLabel(self)
        layout.addWidget(self.description_length, 0, Qt.AlignmentFlag.AlignRight)
        layout.addWidget(QLabel("项目登记信息"))
        form = QFormLayout()
        self.owner = QLineEdit(str(info.get("owner", "")))
        member_values = info.get("members", [])
        self.members = QLineEdit("、".join(name for name in member_values if isinstance(name, str)) if isinstance(member_values, list) else "")
        self.members.setPlaceholderText("多人用中文顿号或逗号分隔")
        self.start_date = QLineEdit(str(info.get("start_date", "")))
        self.start_date.setPlaceholderText("YYYY-MM-DD")
        self.end_date = QLineEdit(str(info.get("end_date", "")))
        self.end_date.setPlaceholderText("YYYY-MM-DD")
        self.priority = QComboBox()
        self.priority.addItem("未设置", "")
        for title, value in (("高", "high"), ("中", "medium"), ("低", "low")):
            self.priority.addItem(title, value)
        index = self.priority.findData(info.get("priority", ""))
        self.priority.setCurrentIndex(max(0, index))
        self.repository = QLineEdit(str(info.get("repository", "")))
        self.repository.setPlaceholderText("https://github.com/owner/repo")
        tag_values = info.get("tags", [])
        self.tags = QLineEdit("、".join(tag for tag in tag_values if isinstance(tag, str)) if isinstance(tag_values, list) else "")
        self.tags.setPlaceholderText("用顿号分隔，例如：个人项目、效率工具、Python")
        self.cover_image = QLineEdit(str(info.get("cover_image", "")))
        self.cover_image.setPlaceholderText("可选：选择本地封面图片")
        cover_row = QHBoxLayout()
        cover_row.addWidget(self.cover_image, 1)
        cover_button = QPushButton("选择图片…")
        cover_button.clicked.connect(self.choose_cover)
        cover_row.addWidget(cover_button)
        form.addRow("项目负责人", self.owner)
        form.addRow("项目成员", self.members)
        form.addRow("开始日期", self.start_date)
        form.addRow("结束日期", self.end_date)
        form.addRow("优先级", self.priority)
        form.addRow("代码仓库", self.repository)
        form.addRow("项目标签", self.tags)
        form.addRow("项目封面", cover_row)
        layout.addLayout(form, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.update_description_length()

    def update_description_length(self):
        length = len(self.description.toPlainText())
        self.description_length.setText(f"{length} / 4000")
        self.description_length.setStyleSheet("color: #d9485f;" if length > 4000 else "")

    def choose_cover(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择项目封面", "", "图片文件 (*.png *.jpg *.jpeg *.webp *.bmp)")
        if path:
            self.cover_image.setText(path)

    def confirm(self):
        if len(self.description.toPlainText()) > 4000:
            QMessageBox.information(self, "项目介绍过长", "请将项目介绍控制在 4000 个字符以内。")
            return
        start, end = self.start_date.text().strip(), self.end_date.text().strip()
        parsed = []
        for value, title in ((start, "开始日期"), (end, "结束日期")):
            if value:
                try:
                    parsed.append(date.fromisoformat(value))
                except ValueError:
                    QMessageBox.information(self, "日期格式不正确", f"{title}请使用 YYYY-MM-DD 格式。")
                    return
            else:
                parsed.append(None)
        if parsed[0] and parsed[1] and parsed[0] > parsed[1]:
            QMessageBox.information(self, "日期范围不正确", "开始日期不能晚于结束日期。")
            return
        repository = self.repository.text().strip()
        if repository:
            parsed_url = urlparse(repository)
            if parsed_url.scheme not in {"https", "http", "ssh", "git"} or not parsed_url.netloc or parsed_url.username or parsed_url.password:
                QMessageBox.information(self, "仓库地址不正确", "请填写不含账号凭据的仓库 URL。")
                return
        self.values = {
            "owner": self.owner.text().strip(),
            "members": [name.strip() for name in re.split(r"[、,，;；]", self.members.text()) if name.strip()][:20],
            "start_date": start,
            "end_date": end,
            "priority": self.priority.currentData(),
            "repository": repository,
            "tags": [tag.strip().lstrip("#") for tag in re.split(r"[、,，;；]", self.tags.text()) if tag.strip()][:12],
            "cover_image": self.cover_image.text().strip(),
        }
        if len(self.values["owner"]) > 100 or any(len(name) > 100 for name in self.values["members"] + self.values["tags"]):
            QMessageBox.information(self, "内容过长", "负责人和成员名称请控制在 100 个字符以内。")
            return
        cover_path = self.values["cover_image"]
        if cover_path and (not Path(cover_path).is_file() or not cover_path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))):
            QMessageBox.information(self, "封面图片无效", "请选择存在的 PNG、JPG、WEBP 或 BMP 图片文件。")
            return
        self.accept()


class DeleteProjectDialog(QDialog):
    def __init__(self, parent, item):
        super().__init__(parent)
        self.setWindowTitle("删除已归档项目")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        warning = QLabel(
            "此操作会永久删除项目完整目录，以及登记的 Obsidian 目录联接；无法在管家中撤销。",
            self,
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        details = QLabel(
            f"项目：{item['name']}\n编号：{item['id']}\n目录：{item['directory']}\n\n"
            "已发编号会保留；统一任务、计划和资源记录不会随项目目录删除。",
            self,
        )
        details.setWordWrap(True)
        layout.addWidget(details)
        layout.addWidget(QLabel(f"请输入项目名称以确认：{item['name']}", self))

        self.confirmation = QLineEdit(self)
        self.confirmation.setAccessibleName("输入项目名称确认删除")
        self.confirmation.setPlaceholderText("准确输入项目名称")
        layout.addWidget(self.confirmation)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok,
            self,
        )
        self.delete_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.delete_button.setText("永久删除")
        self.delete_button.setProperty("danger", True)
        self.delete_button.setEnabled(False)
        self.confirmation.textChanged.connect(
            lambda text: self.delete_button.setEnabled(text == item["name"])
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)


class ProjectPage(QWidget):
    def __init__(self, window, backup_page):
        super().__init__()
        self.window = window
        self.rows = []
        self.document_version = 0
        self.project_detail_task_id = ""
        self.project_detail_cache = {}
        self.pending_project_detail_start = None
        self.project_detail_timer = QTimer(self)
        self.project_detail_timer.setSingleShot(True)
        self.project_detail_timer.setInterval(140)
        self.project_detail_timer.timeout.connect(self._start_pending_project_details)
        self.note_preview_version = 0
        self.notes_project_id = ""
        self.move_task = ""
        self.move_watchers = []
        self.move_selection = None
        self.ai_task_runs = set()
        self.ai_task_jobs = {}
        self.project_document_upload_jobs = set()
        self.milestone_status_jobs = {}
        window.task_completed.connect(self.move_finished)
        window.task_completed.connect(self.ai_task_finished)
        window.task_completed.connect(self.project_document_upload_finished)
        window.task_completed.connect(self.milestone_status_finished)
        self.preferred_project = ""
        self.last_used_project_id = ""
        self.status_text, self.task_text, self.project_catalog = "", "", []
        self.document_errors = {}
        self.project_details = {}
        self.closed = False
        self.milestones_document_exists = False
        self.git_worker = None
        self.git_repositories = []
        self.setObjectName("ProjectPage")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 12, 14, 12)
        layout.setSpacing(12)
        self.project_list = QFrame()
        self.project_list.setObjectName("ProjectSidebar")
        self.project_list.setFixedWidth(294)
        left = QVBoxLayout(self.project_list)
        left.setContentsMargins(4, 0, 4, 0)
        left.setSpacing(10)
        heading = QHBoxLayout()
        heading.setSpacing(9)
        title_icon = QLabel()
        title_icon.setObjectName("ProjectListIcon")
        title_icon.setPixmap(nav_icon(3, "#2675ee").pixmap(24, 24))
        title_icon.setFixedSize(40, 40)
        title_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(title_icon)
        title_group = QVBoxLayout()
        title_group.setSpacing(0)
        title_group.addWidget(label("项目", "Title"))
        heading.addLayout(title_group, 1)
        self.new_button = QPushButton("新建项目")
        self.new_button.setProperty("primary", True)
        self.new_button.setIcon(nav_icon(9))
        self.new_button.clicked.connect(self.create_project)
        heading.addWidget(self.new_button)
        self.obsidian_button = QPushButton("在 Obsidian 中编辑")
        self.obsidian_button.setToolTip("打开当前标签对应的项目原文；笔记请使用目录入口")
        self.obsidian_button.clicked.connect(self.open_in_obsidian)
        self.obsidian_button.hide()
        left.addLayout(heading)
        subtitle = label("管理你的所有项目，跟踪进度，沉淀成果。", "Subtitle")
        subtitle.setWordWrap(True)
        left.addWidget(subtitle)
        toolbar = QHBoxLayout()
        self.root_label = QLabel(self)
        self.root_label.hide()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索项目名称或编号…")
        self.search.textChanged.connect(self.render)
        toolbar.addWidget(self.search, 1)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setToolTip("刷新项目登记与状态")
        # The global button style adds 15 px horizontal padding on each side;
        # 48 px clipped the two-character label in the project search row.
        self.refresh_button.setFixedWidth(68)
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_button)
        left.addLayout(toolbar)
        self.filter = QComboBox()
        for title, value in (("进行中", "current"), ("已归档", "archived")):
            self.filter.addItem(title, value)
        self.filter.currentIndexChanged.connect(self._legacy_filter_changed)
        self.filter.hide()
        self.filter_chips = {}
        filter_row = FlowLayout()
        filter_row.setSpacing(5)
        for key, text in (("active", "进行中"), ("archived", "已归档")):
            chip = QToolButton()
            chip.setObjectName("ProjectFilterChip")
            chip.setText(text)
            chip.setProperty("caption", text)
            chip.setCheckable(True)
            chip.clicked.connect(lambda checked=False, value=key: self.set_state_filter(value))
            filter_row.addWidget(chip)
            self.filter_chips[key] = chip
        self.state_filter = "active"
        self.filter_chips["active"].setChecked(True)
        left.addLayout(filter_row)

        self.myself_status = QLabel(self)
        self.myself_status.hide()
        self.open_project_button = QPushButton("打开项目文件夹")
        self.open_project_button.clicked.connect(self.open_project)
        self.git_status_button = QPushButton("Git 状态")
        self.git_status_button.clicked.connect(self.inspect_git_status)
        self.archive_button = QPushButton("归档项目")
        self.archive_button.clicked.connect(self.move_project)

        self.list_title = label("项目列表 · 0", "TodaySectionTitle")
        left.addWidget(self.list_title)
        self.table = QTableWidget(0, 3)
        self.table.setObjectName("ProjectCards")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().hide()
        self.table.setColumnHidden(1, True)
        self.table.setColumnHidden(2, True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setShowGrid(False)
        self.table.setItemDelegateForColumn(0, ProjectCardDelegate(self.table))
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.verticalHeader().setDefaultSectionSize(106)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        left.addWidget(self.table, 1)
        self.message = QLabel("请选择已有工作区，或选择一个空文件夹初始化。")
        self.message.setObjectName("Subtitle")
        self.message.setWordWrap(True)
        left.addWidget(self.message)
        detail = QWidget()
        detail.setObjectName("ProjectDetail")
        detail.setMinimumWidth(0)
        details = QVBoxLayout(detail)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(9)
        hero = ProjectHero()
        self.hero = hero
        hero.setObjectName("ProjectHero")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(20, 16, 20, 16)
        hero_heading = QHBoxLayout()
        self.project_icon = QLabel()
        self.project_icon.setObjectName("ProjectIcon")
        self.project_icon.setPixmap(nav_icon(3, "#ffffff").pixmap(26, 26))
        self.project_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.project_icon.setFixedSize(48, 48)
        hero_heading.addWidget(self.project_icon)
        hero_text = QVBoxLayout()
        hero_text.setSpacing(3)
        self.project_name = label("选择一个项目", "ProjectName")
        self.project_name.setWordWrap(True)
        self.project_name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.project_identity = label("项目的状态、任务、记录都在这里", "TodayMuted")
        self.project_identity.setWordWrap(True)
        hero_text.addWidget(self.project_name)
        hero_text.addWidget(self.project_identity)
        hero_heading.addLayout(hero_text, 1)
        hero_heading.addWidget(self.open_project_button)
        hero_heading.addWidget(self.git_status_button)
        hero_heading.addWidget(self.obsidian_button)
        hero_heading.addWidget(self.archive_button)
        self.obsidian_button.show()
        hero_layout.addLayout(hero_heading)
        hero_layout.addStretch(1)
        description_heading = QHBoxLayout()
        description_heading.addWidget(label("项目介绍", "TodaySectionTitle"), 1)
        self.delete_project_button = QPushButton("删除项目")
        self.delete_project_button.setProperty("danger", True)
        self.delete_project_button.clicked.connect(self.delete_project)
        description_heading.addWidget(self.delete_project_button)
        self.edit_project_info_button = QPushButton("编辑项目信息")
        self.edit_project_info_button.clicked.connect(self.edit_project_info)
        description_heading.addWidget(self.edit_project_info_button)
        hero_layout.addLayout(description_heading)
        self.description_label = label("由你编写项目介绍，补充项目背景、目标或说明。", "ProjectDescriptionLabel")
        self.description_label.setTextFormat(Qt.TextFormat.PlainText)
        self.description_label.setWordWrap(True)
        self.description_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        hero_layout.addWidget(self.description_label)
        self.project_meta = label("", "TodayMuted")
        self.project_meta.setObjectName("ProjectHeroMeta")
        self.project_meta.setWordWrap(True)
        hero_layout.addWidget(self.project_meta)
        details.addWidget(hero)
        self.metrics = [ProjectMetric(title, color, icon) for title, color, icon in (
            ("当前阶段", COLORS[0], 15), ("清单任务", COLORS[2], 9),
            ("项目日志", COLORS[3], 13), ("关联资源", COLORS[1], 6))]
        self.metrics_row = ProjectMetrics(self.metrics)
        self.documents = QTabWidget()
        self.documents.setObjectName("ProjectTabs")
        self.documents.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.documents.tabBar().setUsesScrollButtons(True)
        self.views = []
        self.document_paths = ["agent/STATUS.md", "agent/TASKS.md", "agent/HANDOFF.md"]
        for name in ("当前状态", "任务入口", "日志与交接"):
            view = QTextBrowser()
            view.setOpenExternalLinks(True)
            view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
            text_option = QTextOption()
            text_option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            view.document().setDefaultTextOption(text_option)
            self.views.append(view)

        self.overview = ProjectOverview(
            self.create_shared_task,
            self.upload_project_resource,
            self.edit_project_info,
            self.show_project_activity,
            lambda: self.documents.setCurrentWidget(self.milestones_page),
            self.toggle_shared_task,
            self.set_milestone_status,
            self.run_ai_task,
            self.open_project_document,
            self.open_project_resource,
            self.upload_project_document,
        )
        self.documents.addTab(self.overview, "概览")
        self.task_page = QWidget()
        task_layout = QVBoxLayout(self.task_page)
        task_layout.setContentsMargins(16, 14, 16, 14)
        task_layout.setSpacing(12)
        task_hint = QLabel("项目任务保存在统一任务库；工作台和计划模块会同步显示。AI 规划任务需从概览交给 Codex 执行；手动任务仍可自行标记完成。")
        task_hint.setWordWrap(True)
        task_layout.addWidget(task_hint)
        task_toolbar = QHBoxLayout()
        task_toolbar.addWidget(QLabel("AI 规划与手动任务分别维护在独立 Markdown 文档中；修改文档后点击同步。"), 1)
        self.sync_task_documents_button = QPushButton("同步任务文档")
        self.sync_task_documents_button.clicked.connect(self.sync_task_documents)
        task_toolbar.addWidget(self.sync_task_documents_button)
        task_layout.addLayout(task_toolbar)
        task_columns = QHBoxLayout()
        task_columns.setSpacing(12)
        self.task_lists = {}
        self.task_add_buttons = {}
        self.task_edit_buttons = {}
        self.task_document_buttons = {}
        for source, title, document_hint, document_path in (
            ("ai", "AI 规划任务", "文档：agent/TASKS-AI.md", "agent/TASKS-AI.md"),
            ("manual", "手动编写任务", "文档：agent/TASKS-MANUAL.md", "agent/TASKS-MANUAL.md"),
        ):
            card_frame = QFrame()
            card_frame.setObjectName("ProjectOverviewCard")
            card_layout = QVBoxLayout(card_frame)
            card_layout.setContentsMargins(14, 12, 14, 12)
            card_layout.setSpacing(8)
            heading = QHBoxLayout()
            heading.addWidget(label(title, "TodaySectionTitle"), 1)
            add_button = QPushButton("＋ 新建任务")
            add_button.clicked.connect(lambda _checked=False, value=source: self.create_shared_task(value))
            heading.addWidget(add_button)
            document_button = QPushButton("打开文档")
            document_button.clicked.connect(lambda _checked=False, path=document_path: self.open_task_source_document(path))
            heading.addWidget(document_button)
            card_layout.addLayout(heading)
            hint = QLabel(document_hint)
            hint.setObjectName("Subtitle")
            card_layout.addWidget(hint)
            task_list = QListWidget()
            task_list.setObjectName("ProjectNotesList")
            task_list.itemDoubleClicked.connect(lambda _item, value=source: self.edit_shared_task(value))
            card_layout.addWidget(task_list, 1)
            edit_button = QPushButton("编辑所选任务")
            edit_button.setEnabled(False)
            edit_button.clicked.connect(lambda _checked=False, value=source: self.edit_shared_task(value))
            task_list.currentItemChanged.connect(lambda *_args: self.update_selection_actions())
            card_layout.addWidget(edit_button, 0, Qt.AlignmentFlag.AlignRight)
            self.task_lists[source] = task_list
            self.task_add_buttons[source] = add_button
            self.task_edit_buttons[source] = edit_button
            self.task_document_buttons[source] = document_button
            task_columns.addWidget(card_frame, 1)
        task_layout.addLayout(task_columns, 1)
        self.documents.addTab(self.task_page, "任务")
        self.project_docs = QTabWidget()
        self.project_docs.addTab(self.views[0], "当前状态")
        self.log_view = ProjectLogView(self.views[2])
        self.project_docs.addTab(self.log_view, "日志与交接")
        self.summary_view = QTextBrowser()
        self.summary_view.setOpenExternalLinks(True)
        self.project_docs.addTab(self.summary_view, "阶段总结")
        prompt_page = QWidget()
        prompt_layout = QVBoxLayout(prompt_page)
        prompt_hint = QLabel("交给 AI 前复制提示词：首次明确目标与验收，后续接续任务；每轮实质进展写入项目动态。")
        prompt_hint.setWordWrap(True)
        prompt_layout.addWidget(prompt_hint)
        self.project_prompt_button = QPushButton("查看 / 复制 AI 提示词")
        self.project_prompt_button.clicked.connect(self.show_project_prompt)
        prompt_layout.addWidget(self.project_prompt_button, 0, Qt.AlignmentFlag.AlignLeft)
        prompt_layout.addStretch()
        self.project_docs.addTab(prompt_page, "AI 提示词")
        project_material_page = QWidget()
        material_layout = QVBoxLayout(project_material_page)
        material_layout.setContentsMargins(16, 14, 16, 14)
        material_layout.setSpacing(10)
        material_hint = QLabel("这里管理你维护的项目资料。上传会将副本放入当前项目的“文档”目录，原文件不会移动或修改；同名文件不会覆盖。")
        material_hint.setWordWrap(True)
        material_layout.addWidget(material_hint)
        material_toolbar = QHBoxLayout()
        self.project_material_directory_label = QLabel("选择项目后查看项目资料目录。")
        self.project_material_directory_label.setObjectName("Subtitle")
        self.project_material_directory_label.setWordWrap(True)
        material_toolbar.addWidget(self.project_material_directory_label, 1)
        self.upload_project_material_button = QPushButton("上传文件")
        self.upload_project_material_button.setProperty("primary", True)
        self.upload_project_material_button.clicked.connect(self.upload_project_document)
        material_toolbar.addWidget(self.upload_project_material_button)
        self.refresh_project_material_button = QPushButton("刷新")
        self.refresh_project_material_button.clicked.connect(self.refresh_project_documents)
        material_toolbar.addWidget(self.refresh_project_material_button)
        self.open_project_material_directory_button = QPushButton("打开资料目录")
        self.open_project_material_directory_button.clicked.connect(self.open_project_documents_directory)
        material_toolbar.addWidget(self.open_project_material_directory_button)
        self.open_project_material_button = QPushButton("打开所选文件")
        self.open_project_material_button.clicked.connect(self.open_selected_project_document)
        material_toolbar.addWidget(self.open_project_material_button)
        self.edit_project_material_button = QPushButton("在 Obsidian 中编辑")
        self.edit_project_material_button.clicked.connect(self.open_selected_project_document)
        material_toolbar.addWidget(self.edit_project_material_button)
        material_layout.addLayout(material_toolbar)
        material_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.project_material_list = QListWidget()
        self.project_material_list.setObjectName("ProjectNotesList")
        self.project_material_list.setMinimumWidth(220)
        self.project_material_list.currentItemChanged.connect(self.preview_project_document)
        self.project_material_list.itemDoubleClicked.connect(lambda _item: self.open_selected_project_document())
        material_splitter.addWidget(self.project_material_list)
        self.project_material_preview = QTextBrowser()
        self.project_material_preview.setObjectName("ProjectMarkdownPreview")
        self.project_material_preview.setOpenExternalLinks(False)
        self.project_material_preview.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        material_splitter.addWidget(self.project_material_preview)
        material_splitter.setStretchFactor(0, 1)
        material_splitter.setStretchFactor(1, 3)
        material_layout.addWidget(material_splitter, 1)
        self.project_material_rows = []
        self.project_material_directory = ""
        self.project_material_preview_version = 0
        self.documents.addTab(project_material_page, "文档")
        notes_page = QWidget()
        notes_layout = QVBoxLayout(notes_page)
        notes_layout.setContentsMargins(18, 18, 18, 18)
        notes_layout.setSpacing(12)
        notes_hint = QLabel("笔记保存在当前项目的“笔记”目录。首次使用请先到设置选择 Obsidian 知识库根目录，之后可直接在 Obsidian 中打开并编写 Markdown 笔记。")
        notes_hint.setWordWrap(True)
        notes_layout.addWidget(notes_hint)
        self.notes_directory_label = QLabel("选择项目后打开它的笔记目录。")
        self.notes_directory_label.setObjectName("Subtitle")
        self.notes_directory_label.setWordWrap(True)
        notes_toolbar = QHBoxLayout()
        notes_toolbar.addWidget(self.notes_directory_label, 1)
        self.refresh_notes_button = QPushButton("刷新列表")
        self.refresh_notes_button.clicked.connect(self.refresh_notes)
        notes_toolbar.addWidget(self.refresh_notes_button)
        self.edit_note_button = QPushButton("在 Obsidian 中编辑")
        self.edit_note_button.setProperty("primary", True)
        self.edit_note_button.clicked.connect(self.open_selected_note)
        notes_toolbar.addWidget(self.edit_note_button)
        notes_layout.addLayout(notes_toolbar)
        notes_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.notes_list = QListWidget()
        self.notes_list.setObjectName("ProjectNotesList")
        self.notes_list.setMinimumWidth(190)
        self.notes_list.currentItemChanged.connect(self.preview_selected_note)
        notes_splitter.addWidget(self.notes_list)
        self.notes_preview = QTextBrowser()
        self.notes_preview.setObjectName("ProjectMarkdownPreview")
        self.notes_preview.setOpenExternalLinks(True)
        self.notes_preview.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        notes_splitter.addWidget(self.notes_preview)
        notes_splitter.setStretchFactor(0, 1)
        notes_splitter.setStretchFactor(1, 3)
        notes_layout.addWidget(notes_splitter, 1)
        self.open_notes_directory_button = QPushButton("打开项目笔记目录")
        self.open_notes_directory_button.clicked.connect(self.open_notes_directory)
        notes_layout.addWidget(self.open_notes_directory_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.documents.insertTab(1, notes_page, "笔记")
        related_page = QWidget()
        self.related_page = related_page
        related_layout = QVBoxLayout(related_page)
        related_hint = QLabel("这里显示与当前项目关联的资源库条目。双击条目可打开资源库中的对应资料。")
        related_hint.setWordWrap(True)
        related_layout.addWidget(related_hint)
        self.related_list = QListWidget()
        self.related_list.setWordWrap(True)
        self.related_list.itemDoubleClicked.connect(self.open_related)
        related_layout.addWidget(self.related_list)
        knowledge_button = QPushButton("查看关联知识库资料")
        knowledge_button.clicked.connect(self.show_project_knowledge)
        related_layout.addWidget(knowledge_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.documents.addTab(related_page, "资源")
        milestones_page = QWidget()
        self.milestones_page = milestones_page
        milestones_layout = QVBoxLayout(milestones_page)
        milestones_layout.setContentsMargins(16, 14, 16, 14)
        milestones_heading = QHBoxLayout()
        milestones_heading.addWidget(QLabel("里程碑由你维护在项目 Markdown 文档中。"), 1)
        self.create_milestones_button = QPushButton("创建空白里程碑文档")
        self.create_milestones_button.clicked.connect(self.create_milestones_document)
        milestones_heading.addWidget(self.create_milestones_button)
        self.edit_milestones_button = QPushButton("在 Obsidian 中编辑")
        self.edit_milestones_button.clicked.connect(self.open_in_obsidian)
        milestones_heading.addWidget(self.edit_milestones_button)
        milestones_layout.addLayout(milestones_heading)
        self.milestones_view = QTextBrowser()
        self.milestones_view.setOpenExternalLinks(True)
        milestones_layout.addWidget(self.milestones_view, 1)
        self.documents.addTab(milestones_page, "里程碑")
        self.documents.addTab(self.project_docs, "Agent 文档")
        self.documents.setMinimumWidth(0)
        self.documents.setMinimumHeight(400)
        self.documents.currentChanged.connect(self.on_project_tab_changed)
        self.project_docs.currentChanged.connect(self.update_selection_actions)
        details.addWidget(self.documents, 1)
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(detail)
        detail_scroll.setMinimumWidth(0)
        layout.addWidget(self.project_list)
        layout.addWidget(detail_scroll, 1)
        self.backup_page = backup_page
        backup_page.setParent(self)
        backup_page.hide()
        self.refresh_myself()
        self.project_rows = []
        self.render()
        self.new_button.setEnabled(bool(self.root()))
        if not self.root():
            self.refresh()
        # Load project files when this page is opened, rather than at startup.

    def render_related(self, item):
        self.related_list.clear()
        linked = self.project_details.get("resources", [])
        known = set()
        for resource in self.project_catalog:
            if resource["id"] in linked or item["id"] in resource["metadata"].get("projects", []):
                cell = QListWidgetItem(TYPES[resource["type"]] + " · " + resource["name"])
                cell.setData(Qt.ItemDataRole.UserRole, ("resource", resource["id"]))
                self.related_list.addItem(cell)
                known.add(resource["id"])
        for identity in linked:
            if identity not in known:
                self.related_list.addItem("暂未找到关联资源：" + identity + "（保留引用）")
        if not self.related_list.count():
            self.related_list.addItem("还没有关联资源。可在资源库条目的“适用项目”中填写本项目编号。")

    def open_related(self, item):
        value = item.data(Qt.ItemDataRole.UserRole)
        if not value:
            return
        _, identity = value
        self.open_project_resource(identity)

    def open_project_resource(self, identity):
        resource = next((item for item in self.project_catalog if item["id"] == identity), None)
        self.show_project_resources()
        if resource:
            self.window.knowledge_page.search.setText(resource["name"])

    def open_project_document(self, relative):
        project = self.selected()
        if not project:
            return
        workspace = self.workspace()
        if relative.startswith("文档/"):
            path = workspace.path(project["path"] + "/" + relative)
            if path.suffix.lower() == ".md":
                self.submit("定位 Obsidian 项目资料", lambda context: project_uri(workspace, project["id"], relative),
                            self.launch_obsidian, persist_result=False)
            elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                QMessageBox.warning(self, "无法打开文件", "没有可用于打开此文件的应用程序：\n" + str(path))
            return
        self.submit("定位 Obsidian Agent 文档", lambda context: project_uri(workspace, project["id"], relative),
                    self.launch_obsidian, persist_result=False)

    def show_project_prompt(self):
        item = self.selected()
        if item and item.get("state") not in {"archived", "unknown"}:
            ProjectPromptDialog(self, item).exec()

    def populate_project_documents(self, report):
        self.project_material_rows = report.get("documents", [])
        self.project_material_directory = report.get("directory", "")
        self.project_material_directory_label.setText(self.project_material_directory or "项目资料目录尚未建立。")
        self.project_material_list.blockSignals(True)
        self.project_material_list.clear()
        for document in self.project_material_rows:
            size = document.get("size", 0)
            size_text = f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / (1024 * 1024):.1f} MB"
            row = QListWidgetItem(f'{document["name"]}  ·  {size_text}')
            row.setData(Qt.ItemDataRole.UserRole, document["relative"])
            row.setToolTip(document["relative"] + "\n修改时间：" + document.get("modified", "未知"))
            self.project_material_list.addItem(row)
        self.project_material_list.blockSignals(False)
        self.project_material_preview.setPlainText(
            f"共 {len(self.project_material_rows)} 个项目资料。选择文件可预览；双击或使用上方按钮打开。"
            if self.project_material_rows else
            "这里是你维护的项目资料区。上传的文件会复制到项目的“文档”目录，原文件不会移动。"
        )
        self.update_selection_actions()

    def refresh_project_documents(self):
        if self.selected():
            self.selection_changed()

    def preview_project_document(self, current, _previous=None):
        project = self.selected()
        relative = current.data(Qt.ItemDataRole.UserRole) if current else None
        self.project_material_preview_version += 1
        version = self.project_material_preview_version
        if not project or not relative:
            return
        workspace = self.workspace()
        def show(report):
            if self.closed or version != self.project_material_preview_version:
                return
            if not isinstance(report, dict):
                self.project_material_preview.setPlainText("预览失败；可尝试打开原文件。")
                return
            path = Path(report["path"])
            if report.get("markdown"):
                self.project_material_preview.document().setBaseUrl(QUrl.fromLocalFile(str(path.parent) + os.sep))
                self.project_material_preview.setMarkdown(report["text"])
            else:
                self.project_material_preview.setPlainText(report["text"])
            self.open_project_material_button.setEnabled(True)
            self.edit_project_material_button.setEnabled(path.suffix.lower() == ".md")
        self.window.submit(None, "预览项目资料", lambda context: workspace.project_document(project["id"], relative),
                           show, persist_result=False)

    def upload_project_document(self):
        project = self.selected()
        if not project:
            return
        if project.get("state") == "archived":
            QMessageBox.information(self, "项目已归档", "请先重新启用项目，再添加项目资料。")
            return
        sources, _ = QFileDialog.getOpenFileNames(self, "上传项目资料（可多选）", "", "所有文件 (*)")
        if not sources:
            return
        workspace = self.workspace()
        def completed(report):
            self.selection_changed()
            uploaded = report.get("uploaded", [])
            failed = report.get("failed", [])
            lines = [f"已复制 {len(uploaded)} / {len(sources)} 个文件到项目文档目录。", "原文件未移动或修改。"]
            if failed:
                lines.append("\n未复制的文件：")
                lines.extend(f"• {entry['name']}：{entry['error']}" for entry in failed[:8])
                if len(failed) > 8:
                    lines.append(f"另有 {len(failed) - 8} 个文件未复制。")
            QMessageBox.information(self, "项目资料上传结果", "\n".join(lines))

        def copy_selected(context):
            report = {"uploaded": [], "failed": []}
            for source in sources:
                context.checkpoint()
                try:
                    report["uploaded"].append(workspace.upload_project_document(project["id"], source, context))
                except (UserError, OSError) as exc:
                    report["failed"].append({"name": Path(source).name, "error": str(exc)})
            return report

        job_id = self.submit(f"复制 {len(sources)} 个项目资料文件",
                             copy_selected, completed, persist_result=False)
        if job_id:
            self.project_document_upload_jobs.add(job_id)
            self.update_selection_actions()

    def project_document_upload_finished(self, job_id, state):
        if job_id not in self.project_document_upload_jobs:
            return
        self.project_document_upload_jobs.discard(job_id)
        if state != "success":
            self.selection_changed()
        self.update_selection_actions()

    def set_milestone_status(self, milestone, completed, expected_text):
        project = self.selected()
        if not project or project.get("state") == "archived" or not milestone.get("status_editable"):
            return False
        workspace = self.workspace()
        identity = project["id"]
        job_id = self.submit(
            "更新里程碑状态",
            lambda context: workspace.set_milestone_status(
                identity, expected_text, milestone["line_number"], milestone["status_column"],
                milestone["status"], completed, context),
            persist_result=False,
        )
        if not job_id:
            return False
        self.milestone_status_jobs[job_id] = identity
        return True

    def milestone_status_finished(self, job_id, state):
        identity = self.milestone_status_jobs.pop(job_id, None)
        if not identity:
            return
        root = self.root()
        if root:
            self.project_detail_cache.pop((str(root.resolve()), identity), None)
        if self.selected() and self.selected()["id"] == identity:
            self.selection_changed()

    def open_project_documents_directory(self):
        project = self.selected()
        if not project:
            return
        workspace = self.workspace()
        def opened(report):
            path = report["path"]
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
                QMessageBox.warning(self, "无法打开目录", "项目资料目录：\n" + path)
        self.submit("打开项目资料目录",
                    lambda context: workspace.ensure_project_documents_directory(project["id"], context), opened,
                    persist_result=False)

    def open_selected_project_document(self):
        row = self.project_material_list.currentItem()
        if row:
            self.open_project_document(row.data(Qt.ItemDataRole.UserRole))

    def show_project_resources(self):
        item = self.selected()
        if item:
            self.window.knowledge_page.set_scope(project=item["id"])
            self.window.show_resource_hub()

    def show_project_knowledge(self):
        item = self.selected()
        if item:
            self.window.knowledge_page.set_scope(project=item["id"])
            self.window.show_resource_hub()

    def upload_project_resource(self):
        item = self.selected()
        if not item or item.get("state") == "archived":
            return
        project_id = item["id"]
        self.window.knowledge_page.set_scope(project=project_id)
        self.window.knowledge_page.add_file(project_id=project_id)

    def show_project_activity(self):
        if self.project_docs.indexOf(self.log_view) < 0:
            return
        self.documents.setCurrentWidget(self.project_docs)
        self.project_docs.setCurrentWidget(self.log_view)

    def with_shared_tasks(self, snapshot, project):
        checklist = dict(snapshot["checklist"])
        migration = self.window.store.setting("legacy_task_import_v1", {})
        project_root = str(self.workspace().root.resolve())
        migrated_projects = set(migration.get("project_ids", [])) if migration.get("projects") == project_root else set()
        imported = self.window.store.planned_task_source_refs()
        occurrences = {}
        existing = []
        for task in checklist.get("tasks", []):
            source = task.get("source", "manual")
            text = task["text"]
            key = (source, text)
            occurrence = occurrences.get(key, 0)
            occurrences[key] = occurrence + 1
            ref = legacy_ref(f"project:{self.workspace().root}:{project['id']}:{source}", "task", text, occurrence)
            if project["id"] not in migrated_projects and ref not in imported:
                existing.append(task)
        linked = self.window.store.planned_tasks(project_id=project["id"], project_root=project_root)
        db_items = [{"text": task["title"], "source": task["source"],
                     "done": task["status"] == "done", "status": task["status"],
                     "task_id": task["id"], "due_date": task["due_date"],
                     "completion_note": task.get("completion_note", ""),
                     "running": task["id"] in self.ai_task_runs}
                    for task in linked]
        tasks = existing + db_items
        ai_tasks = [task for task in tasks if task.get("source") == "ai"]
        manual_tasks = [task for task in tasks if task.get("source") != "ai"]
        done = sum(bool(task["done"]) for task in tasks)
        active = sum(task.get("status") in {"active", "in_progress", "进行中", "处理中"} for task in tasks)
        overdue = sum(task.get("status") in {"overdue", "late", "逾期", "已逾期"} for task in tasks)
        checklist.update({"tasks": tasks, "ai_tasks": ai_tasks, "manual_tasks": manual_tasks,
                          "total": len(tasks), "done": done, "in_progress": active,
                          "pending": max(0, len(tasks) - done - active - overdue), "overdue": overdue,
                          "progress": round(done / len(tasks) * 100) if tasks else 0})
        return {**snapshot, "checklist": checklist}

    def project_tasks_migrated(self, project):
        migration = self.window.store.setting("legacy_task_import_v1", {})
        return (migration.get("projects") == str(self.workspace().root.resolve())
                and project["id"] in set(migration.get("project_ids", [])))

    def populate_shared_project_task_lists(self, project):
        tasks = self.window.store.planned_tasks(
            project_id=project["id"], project_root=str(self.workspace().root.resolve()))
        for source, task_list in self.task_lists.items():
            task_list.blockSignals(True)
            task_list.clear()
            group = [task for task in tasks if task.get("source") == source]
            for task in group:
                state = {"planned": "未开始", "active": "进行中", "done": "已完成"}.get(task["status"], task["status"])
                due = task.get("due_date") or "未设置日期"
                row = QListWidgetItem(f"{task['title']}\n{state} · {due}")
                row.setData(Qt.ItemDataRole.UserRole, task)
                row.setToolTip(task["title"] + ("\n" + task.get("notes", "") if task.get("notes") else ""))
                task_list.addItem(row)
            if not group:
                task_list.addItem("暂无任务")
                task_list.item(0).setFlags(Qt.ItemFlag.NoItemFlags)
            task_list.blockSignals(False)
            self.task_edit_buttons[source].setEnabled(False)

    def edit_shared_task(self, source):
        task_list = self.task_lists[source]
        row = task_list.currentItem()
        task = row.data(Qt.ItemDataRole.UserRole) if row else None
        if not task or not task.get("id"):
            return
        due_date = date.fromisoformat(task["due_date"]) if task.get("due_date") else None
        dialog = UnifiedTaskDialog(self.window, kind=task.get("kind", "task"), due_date=due_date,
                                   project_id=task.get("project_id", ""), title=task.get("title", ""), record=task)
        dialog.project.setEnabled(False)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        self.window.submit(None, "编辑项目任务",
                           lambda context: (context.checkpoint(), self.window.store.update_planned_task(task["id"], values))[1],
                           lambda _updated: self.refresh_project_task_views(), persist_result=False)

    def refresh_project_task_views(self):
        self.refresh()
        self.window.today_page.refresh()
        self.window.planning_page.refresh()

    def on_project_tab_changed(self, index):
        self.update_selection_actions()
        project = self.selected()
        if index == 2 and project and project.get("state") != "archived":
            self.sync_task_documents()

    def sync_task_documents(self):
        project = self.selected()
        if not project:
            return
        identity = project["id"]
        workspace = self.workspace()
        from ..project_task_documents import sync_project_task_documents
        def synced(_report):
            if self.selected() and self.selected()["id"] == identity:
                self.refresh_project_task_views()
        self.submit("同步 AI 与手动任务文档",
                    lambda context: sync_project_task_documents(self.window.store, workspace, identity, context),
                    synced, persist_result=False)

    def open_task_source_document(self, relative):
        project = self.selected()
        if not project:
            return
        identity = project["id"]
        workspace = self.workspace()
        if project.get("state") == "archived":
            self.submit("在 Obsidian 中打开任务文档",
                        lambda _context: project_uri(workspace, identity, relative),
                        self.launch_obsidian, persist_result=False)
            return
        from ..project_task_documents import sync_project_task_documents
        def prepare(context):
            sync_project_task_documents(self.window.store, workspace, identity, context)
            return project_uri(workspace, identity, relative)
        self.submit("在 Obsidian 中打开任务文档", prepare, self.launch_obsidian, persist_result=False)

    def render_shared_project_tasks(self, view, project):
        tasks = self.window.store.planned_tasks(
            project_id=project["id"], project_root=str(self.workspace().root.resolve()))
        lines = ["项目任务统一保存在 SQLite；工作台与计划模块读取相同记录。",
                 "原 agent/TASKS.md 已保留为导入前记录，直接编辑它不会同步到任务库。", ""]
        for source, heading in (("ai", "AI 规划"), ("manual", "手动编写")):
            group = [task for task in tasks if task["source"] == source]
            lines.append(f"## {heading}（{len(group)}）")
            if not group:
                lines.append("暂无任务。")
            for task in group:
                mark = "x" if task["status"] == "done" else " "
                state = {"planned": "未开始", "active": "进行中", "done": "已完成"}.get(task["status"], task["status"])
                due = f" · {task['due_date']}" if task["due_date"] else " · 待排期"
                lines.append(f"- [{mark}] {task['title']} · {state}{due}")
            lines.append("")
        view.setPlainText("\n".join(lines))

    def create_shared_task(self, source="manual"):
        project = self.selected()
        if not project:
            QMessageBox.information(self, "选择项目", "先选择一个项目，再为它创建关联任务。")
            return
        dialog = UnifiedTaskDialog(self.window, project_id=project["id"])
        source_index = dialog.source.findData(source)
        if source_index >= 0:
            dialog.source.setCurrentIndex(source_index)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        def save(context):
            context.checkpoint()
            return self.window.store.add_planned_task(values)
        def saved(_task):
            self.refresh_project_task_views()
        self.window.submit(None, "新建项目关联任务", save, saved, persist_result=False)

    def toggle_shared_task(self, identity, done):
        status = "done" if done else "planned"
        def saved(_):
            self.refresh()
            self.window.today_page.refresh()
            self.window.planning_page.refresh()
        self.window.submit(None, "更新项目任务状态",
                           lambda context: (context.checkpoint(), self.window.store.set_planned_task_status(
                               identity, status, manual_override=True))[1],
                           saved, persist_result=False)

    def run_ai_task(self, identity):
        project = self.selected()
        if not project or project.get("state") == "archived":
            QMessageBox.information(self, "项目只读", "请先选择一个进行中的项目，再执行 AI 规划任务。")
            return
        task = self.window.store.planned_task(identity)
        if not task or task.get("source") != "ai":
            QMessageBox.warning(self, "AI 任务不可用", "此任务不存在或不是 AI 规划任务，请刷新后重试。")
            return
        try:
            workspace = self.workspace()
            workspace_root = str(workspace.root.resolve())
        except (OSError, UserError) as exc:
            QMessageBox.warning(self, "项目工作区不可用", str(exc))
            return
        if task.get("project_id") != project["id"] or task.get("project_root") != workspace_root:
            QMessageBox.warning(self, "项目不匹配", "任务不属于当前项目工作区，未执行。")
            return
        if task.get("status") == "done":
            QMessageBox.information(self, "任务已完成", "已完成的 AI 规划任务不能再次运行。")
            return
        agents = [resource for resource in self.window.store.resources()
                  if resource.kind == "agent" and str(resource.options.get("engine", "")).casefold() == "codex"]
        if not agents:
            QMessageBox.information(self, "需要配置 Codex Agent", "请先在 Agent 模块登记 Codex，并填写 Codex CLI 可执行文件路径。")
            return
        selected = agents[0]
        if len(agents) > 1:
            names = [resource.name for resource in agents]
            chosen, accepted = QInputDialog.getItem(self, "选择执行 Agent", "使用哪个 Codex Agent 执行？", names, 0, False)
            if not accepted:
                return
            selected = agents[names.index(chosen)]
        executable = str(selected.options.get("executable", "")).strip() or shutil.which("codex") or ""
        if not executable:
            QMessageBox.information(self, "需要配置 Codex CLI", "请编辑所选 Agent，在可选启动配置中填写 Codex CLI 的原生可执行文件（例如 codex.exe）。")
            return
        try:
            record = workspace.project(project["id"])
            project_directory = workspace.path(record["path"])
        except (OSError, UserError, KeyError) as exc:
            QMessageBox.warning(self, "项目目录不可用", str(exc))
            return
        if not project_directory.is_dir():
            QMessageBox.warning(self, "项目目录不可用", "没有找到当前项目目录，未启动 Agent。")
            return
        if QMessageBox.question(
                self, "交给 Codex 执行",
                f"让 {selected.name} 执行“{task['title']}”？\n\n"
                f"工作目录：{project_directory}\n"
                "Codex 可在此项目目录中修改文件；命令使用 workspace-write 沙箱。"
                "仅在执行成功并返回完成标记后，管家才会将任务标为已完成。") != QMessageBox.StandardButton.Yes:
            return
        if identity in self.ai_task_runs:
            return
        self.ai_task_runs.add(identity)
        self.refresh_project_task_views()

        def execute(context):
            current = self.window.store.planned_task(identity)
            if not current or current.get("source") != "ai" or current.get("status") == "done":
                raise UserError("AI 任务已变化或已完成，请刷新后重试。")
            self.window.store.set_planned_task_status(identity, "active")
            result = execute_codex_task(executable, project_directory, project["id"], current, context)
            updated = self.window.store.complete_ai_planned_task(identity, result["summary"])
            return {"task_id": identity, "title": updated["title"], "summary": result["summary"],
                    "agent": selected.name}

        def completed(report):
            QMessageBox.information(
                self, "AI 任务已完成",
                f"{report['title']} 已由 {report['agent']} 执行并确认完成，完成记录已保存并同步到 AI 任务文档。\n\n"
                + report["summary"][-1400:],
            )
            self.refresh_project_task_views()

        # Serialize against other operations in this project workspace. The Agent
        # resource is only the executor configuration; the project files are the
        # state being mutated by Codex.
        job_id = self.submit("AI 执行项目任务：" + task["title"], execute, completed)
        if job_id:
            self.ai_task_jobs[job_id] = identity
        else:
            self.ai_task_runs.discard(identity)
            self.refresh_project_task_views()

    def ai_task_finished(self, job_id, state):
        identity = self.ai_task_jobs.pop(job_id, None)
        if not identity:
            return
        self.ai_task_runs.discard(identity)
        if state != "success":
            self.refresh_project_task_views()

    def edit_project_info(self):
        item = self.selected()
        if not item or item["state"] == "archived":
            return
        dialog = ProjectInfoDialog(self, item.get("project_info", {}), item.get("description", ""))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        identity = item["id"]

        def saved(_):
            self.preferred_project = identity
            self.refresh()

        self.submit("保存项目信息",
                    lambda context: self.workspace().set_project_info(
                        identity, dialog.values, context, description=dialog.description.toPlainText()
                    ),
                    saved, persist_result=False)

    def myself_resource(self):
        return next((r for r in self.window.store.resources() if r.kind in {"project", "vault"} and Path(r.options.get("path", "")).name.casefold() == "myself"), None)

    def refresh_myself(self):
        resource = self.myself_resource()
        self.myself_status.setText("myself Vault：" + ("已登记" if resource else "未登记"))

    def myself_action(self, action):
        resource = self.myself_resource()
        if not resource:
            self.window.add_resource("vault")
        elif action == "edit":
            self.window.edit_resource(resource)
        else:
            self.window.perform(resource, action)

    def show_backup_resource(self, resource):
        self.window.open_safety_resource(resource)

    def root(self) -> Path | None:
        value = self.window.store.setting("project_workspace", "")
        return Path(value) if value else None

    def workspace(self) -> ProjectWorkspace:
        root = self.root()
        if not root:
            raise UserError("请先选择项目工作区。")
        return ProjectWorkspace(root)

    def owner(self) -> Resource | None:
        root = self.root()
        return Resource("项目工作区", "project", {"path": str(root)}, hashlib.sha256(str(root).encode()).hexdigest()) if root else None

    def submit(self, title, operation, callback=None, persist_result=True):
        owner = self.owner()
        if owner is None:
            QMessageBox.information(self, "选择工作区", "先选择保存 projects 和 agent 的工作区根目录。")
            return
        def locked(context):
            with self.window.service.locks.acquire(self.window.service.lock_keys(owner)):
                return operation(context)
        return self.window.submit(owner, title, locked, callback, persist_result=persist_result)

    def choose_workspace(self):
        if self.owner() and self.window.is_busy(self.owner().id):
            QMessageBox.information(self, "任务运行中", "请等待本工作区任务结束后切换。")
            return
        selected = QFileDialog.getExistingDirectory(self, "选择项目工作区根目录", str(self.root() or Path.home()))
        if not selected:
            return
        root = Path(selected)
        try:
            workspace = ProjectWorkspace(root)
            if not workspace.registry_path.exists():
                if any(root.iterdir()):
                    raise UserError("这不是规范工作区，也不是空目录。请选择含 agent/projects.json 的根目录，或新建空文件夹。")
                if QMessageBox.question(self, "初始化工作区", "在这个空目录建立 projects、agent 和 Obsidian 项目索引？") != QMessageBox.StandardButton.Yes:
                    return
                self.window.store.set_setting("project_workspace", str(root))
                self.submit("初始化项目工作区", workspace.initialize, lambda report: self.refresh())
            else:
                workspace.registry()
                self.window.store.set_setting("project_workspace", str(root))
                self.refresh()
        except (OSError, UserError) as exc:
            QMessageBox.warning(self, "无法使用工作区", str(exc))

    def refresh(self):
        if self.closed or self.move_watchers:
            return
        root = self.root()
        self.root_label.setText("工作区：" + str(root) if root else "未选择项目工作区")
        self.new_button.setEnabled(bool(root))
        if not root:
            self.project_rows = []
            self.render()
            self.project_name.setText("先选择项目工作区")
            self.description_label.setText("选择项目后，可在这里编写项目介绍。")
            self.message.setText("当前数据位置：" + str(self.window.store.root) + " · 尚未选择工作区，原有项目文件不会被删除。")
            return
        if self.owner() and self.window.is_busy(self.owner().id):
            self.message.setText("项目任务正在运行；完成后可点击“刷新”更新列表。")
            return
        def loaded(report):
            vault_sync = report.get("vault_sync", {})
            milestone_progress = report.get("milestone_progress", {})
            self.project_rows = []
            for project in report["projects"]:
                item = dict(project)
                item["progress"] = milestone_progress.get(item["id"])
                info = item.get("project_info", {})
                if not isinstance(info, dict):
                    info = {}
                item["summary"] = item.get("description", "").strip() or ("已归档" if item.get("state") == "archived" else "进行中")
                start, end = info.get("start_date", ""), info.get("end_date", "")
                item["date_label"] = (str(start) + " – " + str(end)).strip(" –")
                self.project_rows.append(item)
            self.render()
            message = f"{len(self.project_rows)} 个项目 · Obsidian 与管家使用同一份文件；外部修改后可点击“刷新”同步。归档不等于备份。"
            if vault_sync.get("hidden_count"):
                message += f" 已从 Obsidian 隐藏 {vault_sync['hidden_count']} 个归档项目入口。"
            errors = vault_sync.get("errors", [])
            if errors:
                details = "；".join(f"{entry['project_id']}：{entry['message']}" for entry in errors[:3])
                message += f" {len(errors)} 个归档入口未完成同步：{details}"
                if len(errors) > 3:
                    message += "；…"
            self.message.setText(message)
        def load_projects(context):
            workspace = self.workspace()
            vault_sync = workspace.sync_archived_vault_entries(context)
            report = workspace.list_projects(context)
            milestone_progress = {}
            for project in report["projects"]:
                context.checkpoint()
                try:
                    document = workspace.milestones_document(project["id"])
                    milestones = milestone_rows(document["text"]) if document.get("exists") else []
                    milestone_progress[project["id"]] = milestone_snapshot(milestones)["progress"]
                except (OSError, UserError):
                    milestone_progress[project["id"]] = None
            report["vault_sync"] = vault_sync
            report["milestone_progress"] = milestone_progress
            return report
        self.submit("同步归档项目入口并读取登记", load_projects, loaded, persist_result=False)

    def selected(self):
        index = self.table.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def _legacy_filter_changed(self, index):
        mode = self.filter.itemData(index)
        self.state_filter = "archived" if mode == "archived" else "active"
        self._sync_filter_chips()
        self.render()

    def _sync_filter_chips(self):
        for key, button in self.filter_chips.items():
            button.setChecked(key == self.state_filter)

    def set_state_filter(self, value):
        self.state_filter = "archived" if value == "archived" else "active"
        self._sync_filter_chips()
        # Keep the index in sync for archive overview and lifecycle actions.
        index = 1 if self.state_filter == "archived" else 0
        self.filter.blockSignals(True)
        self.filter.setCurrentIndex(index)
        self.filter.blockSignals(False)
        self.render()

    def render(self):
        selected = self.selected()
        query = self.search.text().strip().casefold()
        counts = {"active": 0, "archived": 0}
        for project in self.project_rows:
            state = project.get("state", "unknown")
            counts["archived" if state == "archived" else "active"] += 1
        for key, button in self.filter_chips.items():
            button.setText(f'{button.property("caption")} {counts[key]}')
        self.rows = [p for p in self.project_rows
                     if query in (p["id"] + " " + p["name"]).casefold()
                     and ((p.get("state")) == "archived"
                          if self.state_filter == "archived" else
                          (p.get("state")) != "archived")]
        if self.state_filter != "archived":
            self.rows.sort(key=lambda project: project.get("last_used_at", ""), reverse=True)
        self.list_title.setText(f"项目列表 · {len(self.rows)}")
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        for row, item in enumerate(self.rows):
            state_label = ("已归档" if item["state"] == "archived" else
                           "状态待检查" if item["state"] == "unknown" else "进行中")
            for column, value in enumerate((item["id"], item["name"], state_label)):
                cell = QTableWidgetItem(value)
                cell.setToolTip(item["name"] + "\n" + item["id"] + " · " + state_label + "\n" + (item.get("error") or item["directory"]))
                cell.setData(ROW_DATA, {**item, "position": row, "state_label": state_label})
                self.table.setItem(row, column, cell)
        if self.rows:
            identity = self.preferred_project or (selected["id"] if selected else "")
            self.table.selectRow(next((i for i, p in enumerate(self.rows) if p["id"] == identity), 0))
            self.preferred_project = ""
        self.table.blockSignals(False)
        self.selection_changed()

    def update_selection_actions(self, *_):
        item = self.selected()
        enabled = bool(item) and not self.move_watchers
        archived = bool(item and item["state"] == "archived")
        state_valid = bool(item and item.get("state") != "unknown")
        self.edit_project_info_button.setEnabled(enabled and not archived)
        self.project_prompt_button.setEnabled(enabled and not archived and state_valid)
        current_tab = self.documents.currentWidget()
        has_obsidian_document = any(
            current_tab is page for page in (self.overview, self.milestones_page, self.project_docs)
        ) and not (current_tab is self.project_docs and self.project_docs.currentIndex() == 3)
        self.obsidian_button.setEnabled(enabled and has_obsidian_document)
        self.obsidian_button.setText("在 Obsidian 中编辑")
        self.obsidian_button.setToolTip("打开当前标签对应的项目原文；任务请使用单独任务文档")
        self.sync_task_documents_button.setEnabled(enabled and not archived)
        for source in self.task_lists:
            self.task_add_buttons[source].setEnabled(enabled and not archived)
            row = self.task_lists[source].currentItem()
            task = row.data(Qt.ItemDataRole.UserRole) if row else None
            self.task_edit_buttons[source].setEnabled(enabled and not archived and bool(task and task.get("id")))
            self.task_document_buttons[source].setEnabled(enabled)
        self.open_notes_directory_button.setEnabled(enabled)
        self.refresh_notes_button.setEnabled(enabled)
        self.edit_note_button.setEnabled(enabled and self.documents.currentIndex() == 1 and self.notes_list.currentItem() is not None)
        uploading_documents = bool(self.project_document_upload_jobs)
        self.upload_project_material_button.setEnabled(enabled and not archived and not uploading_documents)
        if self.overview.upload_document_button:
            self.overview.upload_document_button.setEnabled(enabled and not archived and not uploading_documents)
        if self.overview.upload_resource_button:
            self.overview.upload_resource_button.setEnabled(enabled and not archived)
        self.refresh_project_material_button.setEnabled(enabled)
        self.open_project_material_directory_button.setEnabled(enabled and not archived)
        has_material = bool(self.project_material_list.currentItem()) and enabled
        self.open_project_material_button.setEnabled(has_material)
        selected_relative = self.project_material_list.currentItem().data(Qt.ItemDataRole.UserRole) if has_material else ""
        self.edit_project_material_button.setEnabled(bool(selected_relative and selected_relative.lower().endswith(".md")))
        self.create_milestones_button.setEnabled(enabled and not archived and not self.milestones_document_exists)
        self.edit_milestones_button.setEnabled(enabled and self.milestones_document_exists)
        self.open_project_button.setEnabled(enabled)
        self.git_repositories = project_git_repositories(item["directory"]) if item else []
        self.git_status_button.setText("Git 状态" if self.git_repositories else "初始化 Git")
        self.git_status_button.setEnabled(bool(item and not self.git_worker and
                                              (self.git_repositories or Path(item["directory"]).is_dir())))
        self.git_status_button.setToolTip(
            "查看状态并管理暂存、提交、拉取和推送" if self.git_repositories else "在项目目录初始化 Git 仓库"
        )
        self.archive_button.setEnabled(enabled and state_valid)
        self.archive_button.setText("重新启用" if archived else "归档项目")
        self.archive_button.setToolTip(
            "只有有效的项目 STATUS.md 才能归档或重新启用。" +
            ("\n" + str(item.get("error", "请修复项目目录或登记路径后刷新。"))
             if item and not state_valid else "")
        )
        self.delete_project_button.setEnabled(enabled and state_valid and archived)
        self.delete_project_button.setToolTip(
            "仅允许删除已归档项目；点击后还需准确输入项目名称确认。" if state_valid else
            "项目状态待检查，修复项目目录或登记路径后再操作。"
        )

    def selection_changed(self):
        if self.move_watchers:
            return
        self.project_detail_timer.stop()
        self.pending_project_detail_start = None
        if self.project_detail_task_id:
            running = self.window.jobs.get(self.project_detail_task_id)
            if running:
                running[0].context.cancel.set()
            self.project_detail_task_id = ""
        item = self.selected()
        cache_key = (str(self.root().resolve()), item["id"]) if item and self.root() else None
        cached_report = copy.deepcopy(self.project_detail_cache.get(cache_key)) if cache_key else None
        if cache_key and cached_report is not None:
            self.project_detail_cache.pop(cache_key, None)
            self.project_detail_cache[cache_key] = copy.deepcopy(cached_report)
        if cached_report is None:
            self.status_text, self.task_text = "", ""
            self.document_errors = {}
            self.milestones_document_exists = False
            self.milestones_view.setPlainText("读取中…" if item else "选择项目后查看其里程碑文档。")
            self.project_details = {}
        self.update_selection_actions()
        self.document_version += 1
        version = self.document_version
        self.note_preview_version += 1
        self.project_material_preview_version += 1
        self.notes_project_id = item["id"] if item else ""
        self.notes_directory_label.setText(str(Path(item["directory"]) / "笔记") if item else "选择项目后打开它的笔记目录。")
        if cached_report is None:
            self.notes_list.clear()
            self.notes_preview.setPlainText("选择左侧笔记进行预览。" if item else "选择项目后查看笔记。")
            self.project_material_list.clear()
            self.project_material_rows = []
            self.project_material_directory = ""
            self.project_material_directory_label.setText(str(Path(item["directory"]) / "文档") if item else "选择项目后查看项目资料目录。")
            self.project_material_preview.setPlainText("选择项目后查看项目资料。" if item else "选择左侧项目查看资料。")
            for view in self.views:
                view.setPlainText("选择项目查看管理资料。" if not item else "读取中…")
            self.project_name.setText(item["name"] if item else "选择一个项目")
            state_label = ("已归档" if item and item["state"] == "archived" else
                           "状态待检查" if item and item["state"] == "unknown" else "进行中")
            self.project_identity.setText(item["id"] + " · " + state_label if item else "从左侧选择，或新建项目")
            self.project_meta.clear()
            self.hero.set_cover("")
            self.description_label.setText((item.get("description", "").strip() or "尚未编写项目介绍。点击“编辑项目信息”，补充项目背景、目标或说明。") if item else "选择项目后，可在这里编写项目介绍。")
            for metric in self.metrics:
                metric.value.setText("—")
                metric.note.setText("读取中…" if item else "请选择项目")
            self.hero.fit()
        if not item:
            self.status_text, self.task_text = "", ""
            for task_list in self.task_lists.values():
                task_list.clear()
            self.summary_view.clear()
            self.related_list.clear()
            self.project_catalog = []
            self.overview.set_data(
                {"checklist": {"tasks": [], "total": 0, "done": 0, "in_progress": 0,
                               "pending": 0, "overdue": 0, "progress": 0},
                 "recent": "请选择项目查看动态。"},
                {}, {}, [], [], [], False,
            )
            self.milestones_view.setPlainText("选择项目后查看其里程碑文档。")
            return
        root, identity = self.root(), item["id"]
        notes_root = work_root(self.window.store)
        def read(context):
            workspace = ProjectWorkspace(root)
            try:
                project_documents = workspace.project_documents(identity, context)
                project_documents_error = ""
            except (UserError, OSError) as exc:
                project_documents = {"documents": [], "directory": str(root / item["path"] / "文档")}
                project_documents_error = str(exc)
            try:
                notes = workspace.notes(identity, context)["notes"]
                notes_error = ""
            except (UserError, OSError) as exc:
                notes, notes_error = [], str(exc)
            documents, errors = [], []
            for relative in self.document_paths:
                context.checkpoint()
                try:
                    documents.append(workspace.document(identity, relative))
                except (UserError, OSError) as exc:
                    documents.append({"text": "", "path": str(root / item["path"] / relative), "error": str(exc)})
                    errors.append(relative + "：" + str(exc))
            try:
                milestones = workspace.milestones_document(identity)
            except (UserError, OSError) as exc:
                milestones = {"text": "", "path": str(root / item["path"] / "agent/MILESTONES.md"), "exists": False, "error": str(exc)}
            try:
                library = Catalog(notes_root).scan(context)
                errors.extend(library["errors"])
            except (UserError, OSError) as exc:
                library = {"items": []}
                errors.append("资源库：" + str(exc))
            return {"documents": documents, "milestones": milestones, "catalog": library["items"],
                    "project_documents": project_documents, "project_documents_error": project_documents_error,
                    "notes": notes, "notes_error": notes_error, "errors": errors}
        def show(report):
            if self.closed or version != self.document_version:
                return
            self.project_detail_cache[cache_key] = copy.deepcopy(report)
            while len(self.project_detail_cache) > 6:
                self.project_detail_cache.pop(next(iter(self.project_detail_cache)))
            self.project_name.setText(item["name"])
            self.description_label.setText(item.get("description", "").strip() or "尚未编写项目介绍。点击“编辑项目信息”，补充项目背景、目标或说明。")
            self.document_errors = {i: d["error"] for i, d in enumerate(report["documents"]) if d.get("error")}
            milestone_doc = report["milestones"]
            self.milestones_document_exists = bool(milestone_doc.get("exists"))
            self.milestones_view.document().setBaseUrl(QUrl.fromLocalFile(str(Path(milestone_doc["path"]).parent) + os.sep))
            if milestone_doc.get("error"):
                self.milestones_view.setPlainText("里程碑文档读取失败：" + milestone_doc["error"])
            elif self.milestones_document_exists:
                self.milestones_view.setMarkdown(milestone_doc["text"])
            else:
                self.milestones_view.setPlainText("还没有里程碑文档。点击“创建空白里程碑文档”后，可在 Obsidian 中自行编写。")
            self.status_text, self.task_text = report["documents"][0]["text"], report["documents"][1]["text"]
            try:
                self.project_details = project_context(self.status_text, item["state"])
            except UserError as exc:
                self.document_errors[0] = str(exc)
                report["errors"].append("STATUS：" + str(exc))
                self.status_text = ""
                self.project_details = project_context("", item["state"])
            self.project_catalog = report["catalog"]
            self.populate_project_documents(report["project_documents"])
            if report.get("project_documents_error"):
                self.project_material_preview.setPlainText("读取项目资料目录失败：" + report["project_documents_error"])
            self.populate_notes(report["notes"], report.get("notes_error", ""))
            blocks = summaries(report["documents"][2]["text"])
            self.summary_view.setMarkdown("\n\n---\n\n".join(reversed(blocks)) if blocks else "还没有阶段总结。可在 Obsidian 中打开 HANDOFF.md 并自行补充。")
            self.render_related(item)
            snapshot = overview_snapshot(self.status_text, self.task_text, report["documents"][2]["text"], blocks)
            snapshot = self.with_shared_tasks(snapshot, item)
            self.populate_shared_project_task_lists(item)
            phase = self.project_details["phase"]
            lifecycle = ("已归档" if item["state"] == "archived" else
                         "状态待检查" if item["state"] == "unknown" else "进行中")
            self.project_identity.setText(item["id"] + " · " + lifecycle + " · " + phase)
            project_info = item.get("project_info", {})
            if not isinstance(project_info, dict):
                project_info = {}
            date_range = " – ".join(str(value or "未设置") for value in
                                    (project_info.get("start_date", ""), project_info.get("end_date", "")))
            member_values = project_info.get("members", [])
            if not isinstance(member_values, list):
                member_values = []
            members = "、".join(str(name) for name in member_values if isinstance(name, str)) or str(project_info.get("owner", "未设置负责人"))
            tag_values = project_info.get("tags", [])
            if not isinstance(tag_values, list):
                tag_values = []
            tags = "  ·  ".join("#" + str(tag) for tag in tag_values if isinstance(tag, str) and tag)
            self.project_meta.setText("  ·  ".join(part for part in (date_range, members, tags,
                                                                     "最近更新 " + snapshot["updated"]) if part))
            self.hero.set_cover(project_info.get("cover_image", ""))
            if report["errors"]:
                self.project_meta.setText("部分资料需要检查：" + "；".join(report["errors"]))
            linked = [self.related_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.related_list.count())]
            linked_ids = {value[1] for value in linked if value and value[0] == "resource"}
            overview_resources = [resource for resource in self.project_catalog if resource["id"] in linked_ids]
            resources = len(overview_resources)
            checklist = snapshot["checklist"]
            values = (phase, f'{checklist["done"]} / {checklist["total"]}' if checklist["total"] else "任务入口",
                f'{snapshot["logs"]} 条', f'{resources} 项')
            notes = ("可自定义阶段", "清单完成 / 总数" if checklist["total"] else "查看任务原记录",
                "日期日志，保留原文", "关联资源库条目")
            for metric, value, note in zip(self.metrics, values, notes):
                metric.value.setText(value[:7] + "…" if metric is self.metrics[0] and len(value) > 7 else value)
                metric.value.setToolTip(value)
                metric.note.setText(note)
            self.hero.fit()
            self.update_selection_actions()
            self.overview.set_data(snapshot, item, project_info, overview_resources,
                                   self.project_details.get("agents", []),
                                   milestone_rows(milestone_doc.get("text", "")), self.milestones_document_exists,
                                   report["project_documents"].get("documents", []), milestone_doc.get("text", ""))
            self.update_selection_actions()
            for index, (view, document) in enumerate(zip(self.views, report["documents"])):
                view.document().setBaseUrl(QUrl.fromLocalFile(str(Path(document["path"]).parent) + os.sep))
                if index in self.document_errors:
                    view.setPlainText("这份文档暂时无法读取：" + self.document_errors[index] + "\n请打开项目文件夹检查；其他资料仍可查看。原文件未改动。")
                elif index == 2:
                    self.log_view.set_text(document["text"])
                else:
                    view.setMarkdown(document["text"])
        # A dedicated read does not compete with the selected lifecycle task's resource identity.
        def start_loading():
            current = self.selected()
            if not current or current["id"] != identity or version != self.document_version:
                return ""
            if current.get("state") != "archived" and identity != self.last_used_project_id:
                try:
                    used_at = self.workspace().record_project_use(identity)
                except (OSError, UserError) as exc:
                    self.message.setText("未能更新项目使用顺序：" + str(exc))
                else:
                    if used_at:
                        self.last_used_project_id = identity
                        current["last_used_at"] = used_at
                        item["last_used_at"] = used_at
                        for project in self.project_rows:
                            if project["id"] == identity:
                                project["last_used_at"] = used_at
                                break
                        # Apply the recent-use sort on the next list refresh so the
                        # selected card does not jump while the user is switching.
                        for row in range(self.table.rowCount()):
                            cell = self.table.item(row, 0)
                            data = cell.data(ROW_DATA) if cell else None
                            if data and data.get("id") == identity:
                                cell.setData(ROW_DATA, {**data, "last_used_at": used_at})
                                break
            return self.window.submit(None, "读取项目管理文档", read, show, persist_result=False)

        self.pending_project_detail_start = start_loading
        if cached_report is not None:
            show(cached_report)
        self.project_detail_timer.start()

    def _start_pending_project_details(self):
        start = self.pending_project_detail_start
        self.pending_project_detail_start = None
        if start and not self.closed:
            self.project_detail_task_id = start()

    def stop_updates(self):
        self.closed = True
        self.project_detail_timer.stop()
        self.pending_project_detail_start = None
        if self.project_detail_task_id:
            running = self.window.jobs.get(self.project_detail_task_id)
            if running:
                running[0].context.cancel.set()
        if self.git_worker:
            self.git_worker.cancel.set()

    def populate_notes(self, notes, error=""):
        self.notes_list.blockSignals(True)
        self.notes_list.clear()
        for note in notes:
            entry = QListWidgetItem(note["name"])
            entry.setData(Qt.ItemDataRole.UserRole, note["relative"])
            entry.setToolTip(note["relative"])
            self.notes_list.addItem(entry)
        self.notes_list.blockSignals(False)
        if error:
            self.notes_preview.setPlainText("读取项目笔记目录失败：" + error)
        elif notes:
            self.notes_preview.setPlainText("共找到 " + str(len(notes)) + " 篇 Markdown 笔记。选择一篇查看预览；需要修改时点击“在 Obsidian 中编辑”。")
        else:
            self.notes_preview.setPlainText("这个项目的“笔记”目录中还没有 Markdown 笔记。")
        self.update_selection_actions()

    def refresh_notes(self):
        item = self.selected()
        if not item:
            return
        identity = item["id"]
        self.submit("刷新项目笔记列表",
                    lambda context: self.workspace().notes(identity, context)["notes"],
                    lambda notes: self.populate_notes(notes) if self.notes_project_id == identity else None,
                    persist_result=False)

    def preview_selected_note(self, current, _previous=None):
        item = self.selected()
        relative = current.data(Qt.ItemDataRole.UserRole) if current else ""
        if not item or not relative:
            self.update_selection_actions()
            return
        identity = item["id"]
        self.note_preview_version += 1
        version = self.note_preview_version
        self.notes_preview.setPlainText("读取笔记预览…")
        def show(report):
            selected = self.notes_list.currentItem()
            current_project = self.selected()
            if (self.closed or version != self.note_preview_version or self.notes_project_id != identity
                    or not current_project or current_project["id"] != identity
                    or not selected or selected.data(Qt.ItemDataRole.UserRole) != relative):
                return
            self.notes_preview.document().setBaseUrl(QUrl.fromLocalFile(str(Path(report["path"]).parent) + os.sep))
            self.notes_preview.setMarkdown(report["text"])
            self.update_selection_actions()
        self.submit("预览项目笔记", lambda _context: self.workspace().note_document(identity, relative),
                    show, persist_result=False)

    def open_selected_note(self):
        item = self.selected()
        note = self.notes_list.currentItem()
        relative = note.data(Qt.ItemDataRole.UserRole) if note else ""
        if not item or not relative:
            return
        identity = item["id"]
        workspace = self.workspace()
        self.submit("在 Obsidian 中编辑项目笔记",
                    lambda context: project_uri(workspace, identity, relative),
                    self.launch_obsidian, persist_result=False)

    def open_notes_directory(self):
        item = self.selected()
        if not item:
            return
        configured_vault = self.window.store.setting("obsidian_vault_root", "")
        if not configured_vault:
            QMessageBox.information(self, "设置 Obsidian 知识库", "请先到“设置”选择 Obsidian 知识库根目录，再打开项目笔记。")
            return
        workspace = self.workspace()
        def open_notes(context):
            report = project_notes_uri(workspace, item["id"], Path(configured_vault))
            context.checkpoint()
            workspace.ensure_notes_directory(item["id"], context)
            return report
        self.submit("在 Obsidian 中打开项目笔记", open_notes, self.launch_obsidian, persist_result=False)

    def launch_obsidian(self, report):
        if not QDesktopServices.openUrl(QUrl(report["uri"])):
            QMessageBox.warning(self, "未能打开 Obsidian", "请安装并运行 Obsidian 一次以注册 obsidian:// 接口。原文件仍在：\n" + report["path"])

    def open_in_obsidian(self):
        item = self.selected()
        current_tab = self.documents.currentWidget()
        if not item or not any(current_tab is page for page in (self.overview, self.milestones_page, self.project_docs)):
            return
        if current_tab is self.overview:
            relative = "agent/STATUS.md"
        elif current_tab is self.project_docs:
            relative = "agent/STATUS.md" if self.project_docs.currentIndex() == 0 else "agent/HANDOFF.md"
        else:
            relative = "agent/MILESTONES.md"
        workspace = self.workspace()
        self.submit("定位 Obsidian 项目文档", lambda context: project_uri(workspace, item["id"], relative), self.launch_obsidian, persist_result=False)

    def create_milestones_document(self):
        item = self.selected()
        if not item or self.milestones_document_exists:
            return
        self.submit("创建空白里程碑文档",
                    lambda context: self.workspace().create_milestones_document(item["id"], context),
                    lambda _report: self.selection_changed(), persist_result=False)

    def create_project(self, legacy=None):
        if not self.root():
            self.choose_workspace()
            return
        dialog = ProjectDialog(self, legacy if isinstance(legacy, dict) else None)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, goal, entry = dialog.name.text().strip(), dialog.goal.toPlainText().strip(), dialog.entry.isChecked()
        folder_name = dialog.folder_name.text().strip()
        workspace = self.workspace()
        def completed(report):
            self.search.clear()
            self.set_state_filter("active")
            self.preferred_project = report["project_id"]
            self.refresh()
            ProjectPromptDialog(self, {"id": report["project_id"], "name": name,
                                       "directory": report["directory"]}, initial=True).exec()
        initialization = dialog.initialization_selection
        self.submit("初始化新项目", lambda context: workspace.create(name, goal, entry, context, folder_name,
                                                                    initialization), completed)

    def open_project(self):
        item = self.selected()
        if item:
            self.window.open_path(Path(item["directory"]))

    def inspect_git_status(self):
        item = self.selected()
        if not item or self.git_worker:
            return
        project_info = item.get("project_info", {})
        if not isinstance(project_info, dict):
            project_info = {}
        registered_repository = project_info.get("repository", "")
        repositories = project_git_repositories(item["directory"])
        if not repositories:
            if QMessageBox.question(
                self, "初始化 Git", f"要在项目目录初始化 Git 仓库吗？\n\n{item['directory']}\n\n初始化不会自动暂存或提交文件。"
            ) != QMessageBox.StandardButton.Yes:
                return
            self.run_git_action("init", Path(item["directory"]), None, "", registered_repository)
            return
        repository = repositories[0]
        if len(repositories) > 1:
            choices = [path.relative_to(Path(item["directory"]).resolve()).as_posix() or "." for path in repositories]
            selected, accepted = QInputDialog.getItem(self, "选择 Git 仓库", "请选择要查看的仓库：", choices, 0, False)
            if not accepted:
                return
            repository = repositories[choices.index(selected)]
        worker = ReadWorker(lambda cancel: read_project_git_status(repository, cancel, registered_repository))
        self.git_worker = worker
        self.git_status_button.setEnabled(False)
        self.git_status_button.setText("读取中…")

        def finished(report):
            self.git_worker = None
            if self.closed:
                return
            self.git_status_button.setText("Git 状态")
            self.update_selection_actions()
            if report is None:
                QMessageBox.warning(self, item["name"] + " · Git 状态",
                                    "没有收到 Git 状态读取结果，请重试或检查项目目录和 Git 安装。")
                return
            if report["ok"]:
                repo_name = repository.relative_to(Path(item["directory"]).resolve()).as_posix()
                title = item["name"] if repo_name == "." else item["name"] + " · " + repo_name
                dialog = ProjectGitStatusDialog(
                    self, title, repository, report,
                    lambda action, message="", path="": self.run_git_action(action, repository, dialog, message,
                                                                                registered_repository, path),
                )
                dialog.exec()
            else:
                QMessageBox.warning(self, item["name"] + " · Git 状态", report["text"])

        worker.signals.finished.connect(finished)
        self.window.pool.start(worker)

    def run_git_action(self, action, repository, dialog, commit_message, registered_repository="", file_path=""):
        if self.git_worker:
            return
        item = self.selected()
        if not item:
            return
        token = ""
        if action in {"fetch", "pull", "push"}:
            try:
                token = self.window.service.secrets.get("git:https-token") or ""
            except UserError as exc:
                QMessageBox.warning(self, "Git 凭据不可用", str(exc))
                return
        def operation(cancel):
            if action == "refresh":
                status = read_project_git_status(repository, cancel, registered_repository)
                return {"ok": bool(status and status.get("ok")), "message": "状态已刷新", "status": status}
            return run_project_git_action(repository, action, cancel, commit_message, token, registered_repository,
                                          file_path)

        worker = ReadWorker(operation)
        self.git_worker = worker
        self.git_status_button.setEnabled(False)
        if dialog:
            dialog.set_busy(True)
        else:
            self.git_status_button.setText("Git 操作中…")

        def finished(report):
            self.git_worker = None
            if self.closed:
                return
            self.update_selection_actions()
            if report is None:
                if dialog and dialog.isVisible():
                    dialog.set_busy(False, "Git 操作没有返回结果，请刷新状态后检查仓库。")
                else:
                    QMessageBox.warning(self, "Git 操作", "操作没有返回结果，请刷新状态后检查仓库。")
                return
            if not report.get("ok"):
                if dialog and dialog.isVisible():
                    dialog.set_busy(False, report.get("message", "Git 操作未完成。"))
                else:
                    QMessageBox.warning(self, "Git 操作未完成", report.get("message", "请检查 Git 安装、网络和仓库配置。"))
                return
            current = report.get("status")
            if dialog and dialog.isVisible():
                dialog.set_busy(False, report.get("message", "操作完成。"))
                if current and current.get("ok"):
                    if action == "commit":
                        dialog.commit_input.clear()
                    dialog.set_report(current, report.get("message", "操作完成。"))
            elif current and current.get("ok"):
                repo_name = repository.relative_to(Path(item["directory"]).resolve()).as_posix()
                title = item["name"] if repo_name == "." else item["name"] + " · " + repo_name
                new_dialog = ProjectGitStatusDialog(
                    self, title, repository, current,
                    lambda next_action, message="", path="": self.run_git_action(next_action, repository, new_dialog,
                                                                                    message, registered_repository, path),
                )
                new_dialog.exec()
            else:
                QMessageBox.information(self, "Git 操作已完成", report.get("message", "操作完成。"))

        worker.signals.finished.connect(finished)
        self.window.pool.start(worker)

    def move_project(self):
        item = self.selected()
        if not item:
            return
        if item.get("state") == "unknown":
            detail = item.get("error") or (str(Path(item["directory"]) / "agent/STATUS.md"))
            QMessageBox.warning(
                self, "无法归档项目",
                "项目状态文档无法读取，未执行归档。请恢复项目工作区或修正登记路径后刷新。\n\n" + str(detail),
            )
            return
        workspace = self.workspace()
        resume = item["state"] == "archived"
        action = "重新启用" if resume else "归档"
        effect = ("重新建立 Obsidian 项目入口" if resume else "从 Obsidian 的 03-项目目录隐藏项目入口")
        message = f"{action}“{item['name']}”吗？\n\n将更新 STATUS.md 并{effect}。真实项目文件夹、编号和登记路径保留。"
        if QMessageBox.question(self, action + "项目", message) != QMessageBox.StandardButton.Yes:
            return
        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("再次确认" + action)
        confirm.setText(f"请再次确认：{action}项目“{item['name']}”？")
        confirm.setInformativeText("只会移除或重建经核验的 Obsidian 目录联接，不会移动或删除项目文件。")
        confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return
        def completed(_report):
            if resume:
                self.last_used_project_id = ""
            self.set_state_filter("active" if resume else "archived")
            self.preferred_project = item["id"]
            self.refresh()
            self.window.today_page.refresh()
        self.submit(action + "项目", lambda context: workspace.set_archived(item["id"], not resume, context), completed)

    def delete_project(self):
        item = self.selected()
        if not item:
            return
        if item.get("state") == "unknown":
            detail = item.get("error") or (str(Path(item["directory"]) / "agent/STATUS.md"))
            QMessageBox.warning(
                self, "无法删除项目",
                "项目状态文档无法读取，未执行删除。请恢复项目工作区或修正登记路径后刷新。\n\n" + str(detail),
            )
            return
        if item["state"] != "archived":
            QMessageBox.information(self, "请先归档项目", "为避免误删正在使用的项目，请先归档，再执行删除。")
            return
        dialog = DeleteProjectDialog(self, item)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        confirmation = dialog.confirmation.text()
        workspace = self.workspace()
        owner = self.owner()
        if owner and self.window.is_busy(owner.id):
            QMessageBox.information(self, "项目操作进行中", "请等待当前工作区操作完成后再删除。")
            return

        self.pause_move_watches()
        def completed(report):
            if self.closed:
                return
            QMessageBox.information(
                self, "项目已删除",
                f"“{report['name']}”（{report['project_id']}）已从项目登记中移除。\n"
                "项目目录和已登记的 Obsidian 入口已删除；编号仍保留。\n"
                "统一任务、计划和资源记录未删除。",
            )
        task_id = self.submit(
            "删除项目",
            lambda context: workspace.delete_project(item["id"], confirmation, context),
            completed,
        )
        if task_id:
            self.move_task = task_id
            self.move_selection = None
        else:
            self.restore_move_watches()

    def pause_move_watches(self):
        # Windows directory notification handles prevent rename. Today also
        # watches project STATUS files, including while its page is hidden.
        self.document_version += 1
        for watcher in self.window.findChildren(QFileSystemWatcher):
            paths = watcher.files() + watcher.directories()
            self.move_watchers.append((watcher, paths, watcher.signalsBlocked()))
            watcher.setProperty("projectMovePaused", True)
            watcher.blockSignals(True)
            if paths:
                watcher.removePaths(paths)
        self.update_selection_actions()

    def restore_move_watches(self):
        for watcher, paths, blocked in self.move_watchers:
            watcher.setProperty("projectMovePaused", False)
            available = [p for p in paths if Path(p).exists()]
            if available:
                watcher.addPaths(available)
            watcher.blockSignals(blocked)
        self.move_watchers = []
        self.move_task = ""
        self.update_selection_actions()

    def move_finished(self, identity, state):
        if identity != self.move_task:
            return
        selection = self.move_selection
        self.move_selection = None
        self.restore_move_watches()
        if self.closed:
            return
        if state == "success" and selection:
            self.set_state_filter("active" if selection[1] else "archived")
            self.preferred_project = selection[0]
        self.refresh()
        self.window.today_page.refresh()
