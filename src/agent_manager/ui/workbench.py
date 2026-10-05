from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QFileSystemWatcher, QEvent, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextBrowser, QTextEdit, QSplitter, QListWidget, QListWidgetItem, QDialog,
    QDialogButtonBox, QFormLayout, QLineEdit, QComboBox, QCheckBox, QScrollArea, QMessageBox)

from ..domain import UserError
from ..project_workspaces import ProjectWorkspace
from ..runtime import TaskContext
from ..storage import now
from ..workbench import Daily, Catalog, TYPES, work_root, markdown_uri, project_context
from .components import FlowLayout
from .tasks import ReadWorker
from .presentation import readable_time


def control(text, action, primary=False):
    widget = QPushButton(text)
    widget.setProperty("primary", primary)
    widget.clicked.connect(action)
    return widget


def edit_markdown(parent, title, text):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(760, 620)
    layout = QVBoxLayout(dialog)
    hint = QLabel("编辑同一份 Markdown；保存时若原文件已改变，会提示刷新。")
    hint.setWordWrap(True)
    layout.addWidget(hint)
    editor = QTextEdit()
    editor.setAcceptRichText(False)
    editor.setPlainText(text)
    layout.addWidget(editor, 1)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return editor.toPlainText() if dialog.exec() == QDialog.DialogCode.Accepted else None


class MarkdownPage(QWidget):
    """Debounced file watching, one background read, and no body in operation history."""
    def __init__(self, window):
        super().__init__()
        self.window, self.worker, self.closed, self.pending = window, None, False, False
        self.watcher = QFileSystemWatcher(self)
        self.reload_timer = QTimer(self)
        self.reload_timer.setSingleShot(True)
        self.reload_timer.setInterval(350)
        self.reload_timer.timeout.connect(lambda: self.refresh() if self.isVisible() else None)
        self.watcher.fileChanged.connect(lambda *_: self.reload_timer.start())
        self.watcher.directoryChanged.connect(lambda *_: self.reload_timer.start())
        window.installEventFilter(self)

    def read_background(self, operation, apply):
        if self.closed:
            return
        if self.worker:
            self.pending = True
            return
        def read(cancel):
            context = TaskContext()
            context.cancel = cancel
            return operation(context)
        worker = ReadWorker(read)
        self.worker = worker
        def finished(report):
            self.worker = None
            if self.closed:
                return
            if report is None:
                self.read_failed()
            else:
                apply(report)
            if self.pending:
                self.pending = False
                self.reload_timer.start()
        worker.signals.finished.connect(finished)
        self.window.pool.start(worker)

    def read_failed(self):
        QMessageBox.warning(self, "资料读取失败", "请检查资料格式、路径与文件权限。原文件未改变。")

    def watch(self, paths):
        previous = self.watcher.files() + self.watcher.directories()
        if previous:
            self.watcher.removePaths(previous)
        available = []
        for value in paths:
            path = Path(value)
            while not path.exists() and path.parent != path:
                path = path.parent
            if path.exists() and str(path) not in available:
                available.append(str(path))
        if available:
            self.watcher.addPaths(available)

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() == QEvent.Type.WindowActivate and self.isVisible() and not self.closed:
            self.reload_timer.start()
        return super().eventFilter(watched, event)

    def open_obsidian(self, path):
        try:
            uri = markdown_uri(path)
            if not QDesktopServices.openUrl(QUrl(uri)):
                raise UserError("请安装并运行 Obsidian 一次以注册打开接口。")
        except UserError as exc:
            QMessageBox.information(self, "打开 Markdown", str(exc))

    def stop_updates(self):
        self.closed = True
        if self.worker:
            self.worker.cancel.set()
        self.reload_timer.stop()
        self.window.removeEventFilter(self)
        previous = self.watcher.files() + self.watcher.directories()
        if previous:
            self.watcher.removePaths(previous)


class TodayPage(MarkdownPage):
    def __init__(self, window):
        super().__init__(window)
        self.day, self.report, self.projects = date.today(), None, []
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.heading = QLabel()
        self.heading.setWordWrap(True)
        self.heading.setObjectName("Title")
        top.addWidget(self.heading, 1)
        top.addWidget(control("编辑今日计划", self.edit_today, True))
        layout.addLayout(top)
        open_row = QHBoxLayout()
        open_row.addWidget(control("Obsidian 中打开", self.open_daily))
        open_row.addStretch()
        layout.addLayout(open_row)
        note = QLabel("先看今天做什么，再进入项目继续工作。日程和任务与你的 Markdown 共用。")
        note.setWordWrap(True)
        note.setObjectName("Subtitle")
        layout.addWidget(note)
        split = QSplitter(Qt.Orientation.Horizontal)
        plan = QWidget()
        left = QVBoxLayout(plan)
        left.setContentsMargins(0, 12, 6, 0)
        left.addWidget(QLabel("今天的日程"))
        self.schedule = QListWidget()
        self.schedule.setMaximumHeight(180)
        self.schedule.setWordWrap(True)
        left.addWidget(self.schedule)
        left.addWidget(QLabel("今日任务 · 勾选即保存"))
        self.tasks = QListWidget()
        self.tasks.setWordWrap(True)
        self.tasks.itemChanged.connect(self.toggle_task)
        left.addWidget(self.tasks, 1)
        self.daily_hint = QLabel("尚未建立今日计划，点击上方编辑。")
        self.daily_hint.setWordWrap(True)
        self.daily_hint.setObjectName("Subtitle")
        left.addWidget(self.daily_hint)
        split.addWidget(plan)
        projects = QWidget()
        right = QVBoxLayout(projects)
        right.setContentsMargins(6, 12, 0, 0)
        right.addWidget(QLabel("需要继续的项目 · 双击进入"))
        self.continue_projects = QListWidget()
        self.continue_projects.setMinimumHeight(120)
        self.continue_projects.setWordWrap(True)
        self.continue_projects.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.continue_projects.itemDoubleClicked.connect(self.open_project)
        right.addWidget(self.continue_projects, 2)
        right.addWidget(QLabel("最近工作的项目"))
        self.recent_projects = QListWidget()
        self.recent_projects.setWordWrap(True)
        self.recent_projects.setMaximumHeight(85)
        self.recent_projects.itemDoubleClicked.connect(self.open_project)
        right.addWidget(self.recent_projects)
        right.addWidget(QLabel("需要我处理"))
        self.attention = QListWidget()
        self.attention.setWordWrap(True)
        self.attention.setMaximumHeight(100)
        self.attention.itemDoubleClicked.connect(self.open_attention)
        right.addWidget(self.attention)
        self.safety = QLabel()
        self.safety.setWordWrap(True)
        self.safety.setObjectName("Subtitle")
        right.addWidget(self.safety)
        split.addWidget(projects)
        split.setSizes([470, 540])
        layout.addWidget(split, 1)
        bottom = FlowLayout()
        bottom.addWidget(control("进入项目", lambda: window.navigation.setCurrentRow(window.PROJECT)))
        bottom.addWidget(control("打开 Agent", lambda: window.navigation.setCurrentRow(window.AGENT)))
        bottom.addWidget(control("找 Prompt / Skill", lambda: window.navigation.setCurrentRow(window.LIBRARY)))
        bottom.addWidget(control("旧版工作笔记", self.legacy_notes))
        layout.addLayout(bottom)
        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(60_000)
        self.clock_timer.timeout.connect(self.check_day)
        self.clock_timer.start()

    def daily(self):
        return Daily(work_root(self.window.store), self.day)

    def check_day(self):
        if self.day != date.today():
            self.day = date.today()
            self.refresh()

    def refresh(self):
        if self.closed:
            return
        self.day = date.today()
        self.heading.setText(self.day.strftime("%Y 年 %m 月 %d 日") + " · " + "星期" + "一二三四五六日"[self.day.weekday()])
        notes_root = work_root(self.window.store)
        daily, root = Daily(notes_root, self.day), self.window.store.setting("project_workspace", "")
        def read(context):
            report = daily.load()
            context.checkpoint()
            projects, errors, watches = [], [], [str(notes_root), str(Path(report["path"]).parent), report["path"]]
            if root:
                workspace = ProjectWorkspace(Path(root))
                listing = workspace.list_projects(context)
                for item in listing["projects"]:
                    if item["path"].startswith("archive/"):
                        continue
                    try:
                        doc = workspace.document(item["id"], "agent/STATUS.md")
                        projects.append({**item, **project_context(doc["text"], item["state"])})
                        watches.append(doc["path"])
                    except UserError:
                        errors.append(item["name"] + "：项目状态需要检查。")
                watches += [str(workspace.registry_path), str(workspace.registry_path.parent)]
            return {"daily": report, "projects": projects, "errors": errors, "watches": watches}
        self.read_background(read, self.render)

    def render(self, report):
        self.report, self.projects = report["daily"], report["projects"]
        self.schedule.clear()
        self.schedule.addItems([item["text"] for item in self.report["schedule"]] or ["暂无日程。可以直接在今日 Markdown 添加时间和安排。"])
        self.tasks.blockSignals(True)
        self.tasks.clear()
        for row in self.report["tasks"]:
            item = QListWidgetItem(row["text"])
            item.setData(Qt.ItemDataRole.UserRole, row["line"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if row["done"] else Qt.CheckState.Unchecked)
            self.tasks.addItem(item)
        self.tasks.blockSignals(False)
        self.daily_hint.setText("还没有今日计划，点“编辑今日计划”开始。" if self.report["original"] is None else "保存到今日 Markdown；Obsidian 修改后会自动刷新。")
        self.continue_projects.clear()
        for row in self.projects:
            if row["state"] in {"completed", "paused"}:
                continue
            step = row["next_step"]
            preview = step if len(step) <= 100 else step[:99] + "…"
            item = QListWidgetItem(row["name"] + " · " + row["phase"] + "\n下一步：" + preview)
            item.setToolTip(row["name"] + "\n" + step)
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            self.continue_projects.addItem(item)
        if not self.continue_projects.count():
            self.continue_projects.addItem("暂无待继续项目。在“项目”中选择工作区或新建项目。")
        recent = sorted(self.projects, key=lambda p: (self.window.store.evidence("project-used:" + p["id"]) or {}).get("at", ""), reverse=True)
        self.recent_projects.clear()
        for row in recent[:4]:
            used = self.window.store.evidence("project-used:" + row["id"])
            if used:
                item = QListWidgetItem(row["name"] + " · " + readable_time(used["at"]))
                item.setData(Qt.ItemDataRole.UserRole, row["id"])
                self.recent_projects.addItem(item)
        if not self.recent_projects.count():
            self.recent_projects.addItem("进入项目后，这里会显示最近使用。")
        self.refresh_attention(report["errors"])
        self.watch(report["watches"])

    def refresh_attention(self, errors=None):
        self.attention.clear()
        for value in errors or []:
            self.attention.addItem(value)
        for row in self.projects:
            if row["state"] == "paused":
                item = QListWidgetItem(row["name"] + "：已暂停，需要决定是否继续。")
                item.setData(Qt.ItemDataRole.UserRole, ("project", row["id"]))
                self.attention.addItem(item)
        resources = {r.id: r for r in self.window.store.resources()}
        latest = {}
        for task in self.window.store.task_summaries(limit=100):
            if task["resource_id"] in latest:
                continue
            latest[task["resource_id"]] = task
            if task["state"] in {"failed", "interrupted"}:
                resource = resources.get(task["resource_id"])
                item = QListWidgetItem((resource.name if resource else "管家") + "：" + task["title"] + "未完成。")
                item.setData(Qt.ItemDataRole.UserRole, ("task", task["resource_id"]))
                self.attention.addItem(item)
        for resource in resources.values():
            observed = self.window.store.evidence("observe:" + resource.id) or {}
            if observed.get("connected") is False or observed.get("service_state") == "failed":
                item = QListWidgetItem(resource.name + "：连接或网关异常，进入数据安全检查。")
                item.setData(Qt.ItemDataRole.UserRole, ("safety", resource.id))
                self.attention.addItem(item)
        if not self.attention.count():
            self.attention.addItem("目前没有需要处理的异常。")
        health = getattr(self.window, "dashboard_health", [])
        details = [r.name + "：" + (readable_time(h["created_at"]) if h.get("created_at") else "尚未备份") for r, h in health if r.kind in {"hermes_local", "agent"}]
        self.safety.setText(" · ".join(details[:3]) or "重要资料的备份状态和恢复入口在“数据安全”。")

    def edit_today(self):
        if not self.report:
            return
        text = edit_markdown(self, "今日计划 · 日程 / 任务 / 工作记录", self.report["text"])
        if text is not None:
            daily, expected = self.daily(), self.report["original"]
            self.window.submit(None, "保存今日计划", lambda context: daily.save(text, expected), lambda _: self.refresh(), persist_result=False)

    def toggle_task(self, item):
        if not self.report:
            return
        daily, expected, line = self.daily(), self.report["original"], item.data(Qt.ItemDataRole.UserRole)
        done = item.checkState() == Qt.CheckState.Checked
        self.window.submit(None, "更新今日任务", lambda context: daily.toggle(line, done, expected), lambda _: self.refresh(), persist_result=False)
        # A second click waits for the first save, so it cannot overwrite another checkbox.
        self.tasks.setEnabled(False)
        QTimer.singleShot(400, self.enable_tasks)

    def enable_tasks(self):
        if self.closed:
            return
        if self.window.jobs or self.worker:
            QTimer.singleShot(200, self.enable_tasks)
        else:
            self.tasks.setEnabled(True)
            self.refresh()

    def open_daily(self):
        if self.report:
            self.open_obsidian(self.report["path"])

    def open_project(self, item):
        identity = item.data(Qt.ItemDataRole.UserRole)
        if identity:
            self.window.open_project_identity(identity)

    def open_attention(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        kind, identity = data
        if kind == "project":
            self.window.open_project_identity(identity)
        else:
            resource = next((r for r in self.window.store.resources() if r.id == identity), None)
            if kind == "task":
                self.window.show_resource_activity(resource)
            elif resource:
                self.window.open_safety_resource(resource)

    def legacy_notes(self):
        from ..workbench import MarkdownFiles
        try:
            text = MarkdownFiles(self.window.store.root).read("work-summary.md") or "# 旧版工作笔记\n\n暂无旧版笔记。"
        except UserError as exc:
            QMessageBox.information(self, "旧版工作笔记", str(exc))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("旧版工作笔记 · 原文件保留")
        dialog.resize(700, 520)
        layout = QVBoxLayout(dialog)
        view = QTextBrowser()
        view.setMarkdown(text)
        layout.addWidget(view)
        layout.addWidget(control("关闭", dialog.accept))
        dialog.exec()

    def stop_updates(self):
        self.clock_timer.stop()
        super().stop_updates()


class CatalogDialog(QDialog):
    def __init__(self, parent, item=None):
        super().__init__(parent)
        self.setWindowTitle("编辑资源" if item else "收藏新资源")
        self.resize(760, 700)
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        panel = QWidget()
        form = QFormLayout(panel)
        metadata = item["metadata"] if item else {}
        self.fields = {}
        self.kind = QComboBox()
        for key, label in TYPES.items():
            self.kind.addItem(label, key)
        self.kind.setCurrentIndex(self.kind.findData(item["type"] if item else "prompt"))
        form.addRow("类型", self.kind)
        labels = {"name": "名称", "tags": "标签（逗号分隔）", "summary": "一句话用途", "source": "来源链接",
                  "local_path": "本地路径", "agents": "适用 Agent（编号 / 类型，逗号分隔）", "projects": "适用项目（编号，逗号分隔）", "notes": "我的备注"}
        for key, label in labels.items():
            value = metadata.get(key, item["name"] if item and key == "name" else "")
            field = QLineEdit(", ".join(value) if isinstance(value, list) else str(value))
            self.fields[key] = field
            form.addRow(label, field)
        self.flags = {}
        row = QWidget()
        flags = FlowLayout(row)
        for key, label in (("favorite", "收藏"), ("installed", "已安装"), ("configured", "已配置"), ("tested", "已测试")):
            check = QCheckBox(label)
            check.setChecked(bool(metadata.get(key)))
            self.flags[key] = check
            flags.addWidget(check)
        form.addRow("状态", row)
        self.body = QTextEdit()
        self.body.setAcceptRichText(False)
        self.body.setMinimumHeight(210)
        self.body.setPlainText(item["body"] if item else "## 用途\n\n## 使用 / 安装说明\n\n## 配置说明\n\n")
        form.addRow("Markdown 正文", self.body)
        scroll.setWidget(panel)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存资源")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def confirm(self):
        if not self.fields["name"].text().strip():
            QMessageBox.information(self, "资源名称", "请填写名称。")
            return
        self.accept()

    def metadata(self):
        data = {key: field.text().strip() for key, field in self.fields.items()}
        for key in ("tags", "agents", "projects"):
            data[key] = [v.strip() for v in re.split(r"[,，]", data[key]) if v.strip()]
        data.update({key: check.isChecked() for key, check in self.flags.items()})
        data["type"] = self.kind.currentData()
        return data


class CatalogPage(MarkdownPage):
    def __init__(self, window):
        super().__init__(window)
        self.catalog, self.items, self.project_filter, self.agent_filter = Catalog(work_root(window.store)), [], "", ""
        layout = QVBoxLayout(self)
        heading = QLabel("资源库")
        heading.setObjectName("Title")
        layout.addWidget(heading)
        note = QLabel("把 Prompt、Skill、MCP 和常用资料放在一起。先收藏、整理和找到；正文由 Markdown 保存。")
        note.setWordWrap(True)
        note.setObjectName("Subtitle")
        layout.addWidget(note)
        row = QHBoxLayout()
        row.addWidget(control("收藏新资源", lambda: self.edit_item(None), True))
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称、标签、用途和正文")
        self.search.textChanged.connect(self.render)
        row.addWidget(self.search, 1)
        self.kind = QComboBox()
        self.kind.addItem("全部类型", "")
        for key, label in TYPES.items():
            self.kind.addItem(label, key)
        self.kind.currentIndexChanged.connect(self.render)
        row.addWidget(self.kind)
        self.favorite = QCheckBox("只看收藏")
        self.favorite.toggled.connect(self.render)
        row.addWidget(self.favorite)
        row.addWidget(control("刷新", self.refresh))
        layout.addLayout(row)
        self.scope = QLabel()
        self.scope.setWordWrap(True)
        layout.addWidget(self.scope)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.listing = QListWidget()
        self.listing.setWordWrap(True)
        self.listing.currentItemChanged.connect(self.selected_changed)
        split.addWidget(self.listing)
        preview = QWidget()
        pane = QVBoxLayout(preview)
        pane.setContentsMargins(6, 0, 0, 0)
        actions = FlowLayout()
        self.edit_button = control("编辑资源", lambda: self.edit_item(self.selected()))
        actions.addWidget(self.edit_button)
        self.favorite_button = control("收藏 / 取消收藏", self.toggle_favorite)
        actions.addWidget(self.favorite_button)
        self.obsidian_button = control("Obsidian 中打开", lambda: self.open_obsidian(self.selected()["path"]) if self.selected() else None)
        actions.addWidget(self.obsidian_button)
        actions.addWidget(control("打开来源", self.open_source))
        actions.addWidget(control("查看本地目录", self.open_local))
        actions.addWidget(control("清除项目 / Agent 筛选", self.clear_scope))
        pane.addLayout(actions)
        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(False)
        self.preview.anchorClicked.connect(self.safe_url)
        pane.addWidget(self.preview, 1)
        split.addWidget(preview)
        split.setSizes([330, 660])
        layout.addWidget(split, 1)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setObjectName("Subtitle")
        layout.addWidget(self.message)

    def refresh(self):
        if self.closed:
            return
        root = work_root(self.window.store)
        if self.catalog.files.root != root.resolve():
            self.catalog = Catalog(root)
        catalog = self.catalog
        self.read_background(catalog.scan, self.loaded)

    def loaded(self, report):
        self.items = report["items"]
        self.message.setText("；".join(report["errors"]) if report["errors"] else f"{len(self.items)} 个资源 · 正文保存为 Markdown，与 Obsidian 使用同一份文件。")
        self.render()
        self.watch([str(self.catalog.files.root), str(self.catalog.files.root / "资源"), *[i["path"] for i in self.items]])

    def render(self, *_):
        selected = self.selected()
        identity = selected["id"] if selected else ""
        self.listing.blockSignals(True)
        self.listing.clear()
        query, kind = self.search.text().casefold().strip(), self.kind.currentData()
        for entry in self.items:
            metadata = entry["metadata"]
            if kind and entry["type"] != kind or self.favorite.isChecked() and not metadata.get("favorite"):
                continue
            if self.project_filter and self.project_filter not in metadata.get("projects", []) and entry["id"] not in self.project_resource_ids():
                continue
            if self.agent_filter and not self.matches_agent(metadata.get("agents", [])):
                continue
            if query and query not in (entry["name"] + " " + str(metadata) + " " + entry["body"]).casefold():
                continue
            cell = QListWidgetItem(("★ " if metadata.get("favorite") else "") + entry["name"] + "\n" + TYPES[entry["type"]] + " · " + ", ".join(metadata.get("tags", [])))
            cell.setData(Qt.ItemDataRole.UserRole, entry)
            self.listing.addItem(cell)
            if entry["id"] == identity:
                self.listing.setCurrentItem(cell)
        if self.listing.currentRow() < 0 and self.listing.count():
            self.listing.setCurrentRow(0)
        self.listing.blockSignals(False)
        self.scope.setText("当前筛选：" + (self.project_filter or self.agent_filter) if self.project_filter or self.agent_filter else "")
        self.selected_changed()

    def project_resource_ids(self):
        return getattr(self, "related_ids", [])

    def matches_agent(self, references):
        resource = next((r for r in self.window.store.resources() if r.id == self.agent_filter), None)
        keys = {self.agent_filter.casefold()}
        if resource:
            engine = resource.options.get("engine", "hermes")
            keys.update({engine.casefold(), engine.casefold().replace(" ", "-")})
            if engine == "Claude Code":
                keys.add("claude")
        return bool(keys & {value.casefold() for value in references})

    def selected(self):
        cell = self.listing.currentItem()
        return cell.data(Qt.ItemDataRole.UserRole) if cell else None

    def selected_changed(self, *_):
        item = self.selected()
        for widget in (self.edit_button, self.favorite_button, self.obsidian_button):
            widget.setEnabled(item is not None)
        if not item:
            self.preview.setPlainText("还没有匹配资源。点击“收藏新资源”，或将带 frontmatter 的 Markdown 放入工作台的“资源”文件夹。")
            return
        metadata = item["metadata"]
        status = " · ".join(label + ("：是" if metadata.get(key) else "：否") for key, label in (("configured", "已配置"), ("tested", "已测试")))
        text = "# " + item["name"] + "\n\n" + str(metadata.get("summary", "")) + "\n\n" + status + "\n\n" + item["body"]
        if metadata.get("notes"):
            text += "\n\n## 我的备注\n\n" + str(metadata["notes"])
        self.preview.setMarkdown(text)

    def edit_item(self, item):
        dialog = CatalogDialog(self, item)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        metadata, body, catalog = dialog.metadata(), dialog.body.toPlainText(), self.catalog
        self.window.submit(None, "保存资源 Markdown", lambda context: catalog.save(metadata, body, item), lambda _: self.refresh(), persist_result=False)

    def toggle_favorite(self):
        item, catalog = self.selected(), self.catalog
        if item:
            self.window.submit(None, "更新资源收藏", lambda context: catalog.save({"favorite": not item["metadata"].get("favorite")}, item["body"], item), lambda _: self.refresh(), persist_result=False)

    def safe_url(self, url):
        if url.scheme() not in {"https", "http"} or not url.host() or url.userName() or url.password():
            QMessageBox.information(self, "打开来源", "只支持不含登录凭据的 http / https 链接。")
            return
        QDesktopServices.openUrl(url)

    def open_source(self):
        item = self.selected()
        if item:
            self.safe_url(QUrl(str(item["metadata"].get("source", ""))))

    def open_local(self):
        item = self.selected()
        if item:
            value = str(item["metadata"].get("local_path", ""))
            path = Path(value) if value else Path(item["path"]).parent
            self.window.open_path(path if path.is_dir() else path.parent)

    def clear_scope(self):
        self.project_filter, self.agent_filter, self.related_ids = "", "", []
        self.render()


class AgentPage(QWidget):
    """Daily entry points for the same registered objects used by existing adapters."""
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        heading = QLabel("Agent")
        heading.setObjectName("Title")
        layout.addWidget(heading)
        note = QLabel("打开常用入口，查看记录和相关资源。Hermes 的网关与恢复管理保留在数据安全中。")
        note.setWordWrap(True)
        note.setObjectName("Subtitle")
        layout.addWidget(note)
        actions = FlowLayout()
        actions.addWidget(control("登记 Agent", lambda: window.add_resource("agent"), True))
        actions.addWidget(control("登记本地 Hermes", lambda: window.add_resource("hermes_local")))
        actions.addWidget(control("登记服务器 Hermes", lambda: window.add_resource("hermes_server")))
        actions.addWidget(control("刷新", self.refresh))
        layout.addLayout(actions)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.listing = QListWidget()
        self.listing.setWordWrap(True)
        self.listing.currentItemChanged.connect(self.render)
        split.addWidget(self.listing)
        panel = QWidget()
        pane = QVBoxLayout(panel)
        row = FlowLayout()
        for text, action in (("打开常用入口", self.open_agent), ("查看记录", self.records),
                             ("相关资源", self.resources), ("写备注", self.notes), ("编辑登记", self.edit), ("查看目录", self.directory), ("备份与恢复", self.safety)):
            row.addWidget(control(text, action))
        pane.addLayout(row)
        self.details = QTextBrowser()
        self.details.setOpenExternalLinks(False)
        pane.addWidget(self.details, 1)
        split.addWidget(panel)
        split.setSizes([300, 680])
        layout.addWidget(split, 1)

    def selected(self):
        item = self.listing.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def refresh(self, selected_id=None):
        previous = self.selected()
        identity = selected_id or (previous.id if previous else "")
        self.listing.blockSignals(True)
        self.listing.clear()
        for resource in self.window.store.resources():
            if resource.kind not in {"agent", "hermes_local", "hermes_server"}:
                continue
            label = resource.options.get("engine", "服务器 Hermes" if resource.kind == "hermes_server" else "Hermes")
            item = QListWidgetItem(resource.name + "\n" + label + " · 已登记")
            item.setData(Qt.ItemDataRole.UserRole, resource)
            self.listing.addItem(item)
            if resource.id == identity:
                self.listing.setCurrentItem(item)
        if self.listing.currentRow() < 0 and self.listing.count():
            self.listing.setCurrentRow(0)
        self.listing.blockSignals(False)
        self.render()

    def render(self, *_):
        resource = self.selected()
        if not resource:
            self.details.setPlainText("先登记你的 Agent。已有登记、备份和配置会自动显示。")
            return
        used = self.window.store.evidence("agent-used:" + resource.id) or {}
        health = next((h for r, h in getattr(self.window, "dashboard_health", []) if r.id == resource.id), {})
        options = resource.options
        text = "# " + resource.name + "\n\n已登记 · " + options.get("engine", "Hermes")
        text += "\n\n最近使用：" + (readable_time(used["at"]) if used else "尚未从管家打开")
        text += "\n\n最近备份：" + readable_time(health.get("created_at") or "尚未备份")
        for key, label in (("install_path", "安装 / 运行位置"), ("path", "项目目录"), ("home", "数据目录"), ("config_dir", "配置目录"), ("entry_url", "常用入口")):
            if options.get(key):
                text += "\n\n" + label + "：`" + str(options[key]).replace("`", "") + "`"
        for path in options.get("record_paths", []):
            text += "\n\n记录目录：`" + path.replace("`", "") + "`"
        text += "\n\n## 备注\n\n" + str(options.get("notes", "暂无备注。"))
        self.details.setMarkdown(text)

    def used(self, resource):
        self.window.store.save_evidence("agent-used:" + resource.id, {"at": now()})
        self.render()

    def open_agent(self):
        resource = self.selected()
        if not resource:
            return
        entry = resource.options.get("entry_url", "")
        if entry:
            url = QUrl(entry)
            if url.scheme() not in {"https", "http"} or not url.host() or url.userName() or url.password():
                QMessageBox.information(self, "常用入口", "入口只支持 http / https 链接。应用启动程序可在登记中配置。")
                return
            if QDesktopServices.openUrl(url):
                self.used(resource)
        elif resource.options.get("executable") and resource.kind != "hermes_server":
            self.window.perform(resource, "start")
            self.used(resource)
        elif resource.kind == "hermes_server":
            self.safety()
        else:
            value = self.local_directory(resource)
            if value:
                self.window.open_path(Path(value))
                self.used(resource)

    def records(self):
        resource = self.selected()
        if resource:
            self.window.perform(resource, "records" if resource.kind == "agent" else "logs" if resource.kind == "hermes_server" else "library")

    def resources(self):
        resource = self.selected()
        if resource:
            self.window.catalog_page.agent_filter = resource.id
            self.window.catalog_page.project_filter = ""
            self.window.navigation.setCurrentRow(self.window.LIBRARY)

    def edit(self):
        if self.selected():
            self.window.edit_resource(self.selected())

    def directory(self):
        resource = self.selected()
        if resource:
            if resource.kind == "hermes_server":
                self.safety()
                self.window.resource_pages[1].show_paths()
            else:
                value = self.local_directory(resource)
                if value:
                    self.window.open_path(Path(value))

    def local_directory(self, resource):
        value = resource.options.get("path") or resource.options.get("home") or (resource.options.get("record_paths") or [""])[0]
        if not value:
            QMessageBox.information(self, "补充 Agent 入口", "在“编辑登记”中选择项目或资料目录，或填写常用入口。当前登记已保留。")
        return value

    def notes(self):
        resource = self.selected()
        if not resource:
            return
        page = self.window.catalog_page
        catalog = Catalog(work_root(self.window.store))
        def loaded(report):
            item = next((i for i in report["items"] if i["type"] == "agent" and resource.id in i["metadata"].get("agents", [])), None)
            if item:
                page.edit_item(item)
            else:
                dialog = CatalogDialog(self)
                dialog.fields["name"].setText(resource.name + " · 使用说明与备注")
                dialog.kind.setCurrentIndex(dialog.kind.findData("agent"))
                dialog.fields["agents"].setText(resource.id)
                if dialog.exec() == QDialog.DialogCode.Accepted:
                    metadata, body = dialog.metadata(), dialog.body.toPlainText()
                    self.window.submit(None, "保存 Agent 备注 Markdown", lambda context: catalog.save(metadata, body), lambda _: page.refresh(), persist_result=False)
        self.window.submit(None, "定位 Agent 备注", catalog.scan, loaded, persist_result=False)

    def safety(self):
        if self.selected():
            self.window.open_safety_resource(self.selected())
