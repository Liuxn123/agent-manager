from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QFileSystemWatcher, QEvent, QUrl, QDate, QSize
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QApplication, QFrame,
    QTextBrowser, QTextEdit, QSplitter, QListWidget, QListWidgetItem, QDialog,
    QDialogButtonBox, QFormLayout, QLineEdit, QComboBox, QCheckBox, QScrollArea, QMessageBox,
    QGridLayout, QDateEdit, QTabWidget, QSizePolicy)

from ..domain import UserError
from ..project_workspaces import ProjectWorkspace
from ..runtime import TaskContext
from ..storage import now
from ..workbench import Agenda, Daily, Catalog, TYPES, work_root, markdown_uri, project_context
from .components import FlowLayout
from .tasks import ReadWorker
from .presentation import readable_time
from .today_widgets import Metric, Section, TodayList, DailyEntryDialog, BackupButton, ROW_DATA, COLORS, link, label


def control(text, action, primary=False):
    widget = QPushButton(text)
    widget.setProperty("primary", primary)
    widget.clicked.connect(action)
    return widget


def edit_markdown(parent, title, text, hint="编辑同一份 Markdown；保存时若原文件已改变，会提示刷新。"):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(760, 620)
    layout = QVBoxLayout(dialog)
    hint_label = QLabel(hint)
    hint_label.setWordWrap(True)
    layout.addWidget(hint_label)
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
        self.auto_refresh = True
        self.watcher = QFileSystemWatcher(self)
        self.reload_timer = QTimer(self)
        self.reload_timer.setSingleShot(True)
        self.reload_timer.setInterval(350)
        self.reload_timer.timeout.connect(lambda: self.refresh() if self.auto_refresh and self.isVisible() else None)
        self.watcher.fileChanged.connect(lambda *_: self.reload_timer.start() if self.auto_refresh else None)
        self.watcher.directoryChanged.connect(lambda *_: self.reload_timer.start() if self.auto_refresh else None)
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
                if self.auto_refresh:
                    self.reload_timer.start()
                else:
                    self.reload_timer.stop()
                    QTimer.singleShot(0, self.refresh)
        worker.signals.finished.connect(finished)
        self.window.pool.start(worker)

    def read_failed(self):
        QMessageBox.warning(self, "资料读取失败", "请检查资料格式、路径与文件权限。原文件未改变。")

    def watch(self, paths):
        if self.watcher.property("projectMovePaused"):
            return
        previous = self.watcher.files() + self.watcher.directories()
        if not self.auto_refresh:
            if previous:
                self.watcher.removePaths(previous)
            return
        available = []
        for value in paths:
            path = Path(value)
            while not path.exists() and path.parent != path:
                path = path.parent
            if path.exists() and str(path) not in available:
                available.append(str(path))
        removed = [value for value in previous if value not in available]
        added = [value for value in available if value not in previous]
        if removed:
            self.watcher.removePaths(removed)
        if added:
            self.watcher.addPaths(added)

    def eventFilter(self, watched, event):
        if self.auto_refresh and watched is self.window and event.type() == QEvent.Type.WindowActivate and self.isVisible() and not self.closed:
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
        self.auto_refresh = False
        self.setObjectName("TodayPage")
        self.day, self.report, self.projects = date.today(), None, []
        self.follow_today = True
        self.loaded_once = False
        self.project_errors = []
        self.todo_index = None
        self.todo_root = None
        self.narrow = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        top = QHBoxLayout()
        self.heading = label("", "TodayDate")
        self.heading.setWordWrap(True)
        top.addWidget(self.heading, 1)
        self.agenda_button = control("日程总览", self.open_agenda)
        top.addWidget(self.agenda_button)
        self.edit_button = control("编辑今日计划", self.edit_today, True)
        top.addWidget(self.edit_button)
        self.refresh_button = control("刷新", self.manual_refresh)
        self.refresh_button.setToolTip("读取 Markdown / Obsidian 最新内容")
        top.addWidget(self.refresh_button)
        layout.addLayout(top)
        note = QHBoxLayout()
        note.addWidget(control("‹", lambda: self.set_day(self.day - timedelta(days=1))))
        self.date_picker = QDateEdit(QDate.currentDate())
        self.date_picker.setCalendarPopup(True)
        self.date_picker.setDisplayFormat("yyyy-MM-dd")
        self.date_picker.setToolTip("选择日期，查看或安排过去与未来的任务和日程。")
        self.date_picker.dateChanged.connect(lambda value: self.set_day(value.toPython()))
        note.addWidget(self.date_picker)
        note.addWidget(control("›", lambda: self.set_day(self.day + timedelta(days=1))))
        self.today_button = control("回到今天", lambda: self.set_day(date.today()))
        note.addWidget(self.today_button)
        note.addStretch()
        layout.addLayout(note)
        self.metric_grid = QGridLayout()
        self.metric_grid.setSpacing(12)
        self.metrics = [Metric(title, color, icon, hint) for title, color, icon, hint in zip(
            ("今日要做", "需要处理", "推进项目"), (COLORS[0], COLORS[2], COLORS[3]), (9, 11, 3),
            ("勾选即保存", "项目与资料提醒", "点击项目继续"))]
        self.metrics[0].add_progress()
        for i, widget in enumerate(self.metrics):
            self.metric_grid.addWidget(widget, 0, i)
        layout.addLayout(self.metric_grid)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName("TodayScroll")
        self.body = QWidget()
        self.body.setObjectName("TodayBody")
        self.columns_layout = QGridLayout(self.body)
        self.columns_layout.setContentsMargins(0, 0, 2, 0)
        self.columns_layout.setSpacing(14)
        self.columns = [QWidget(), QWidget()]
        left, right = [QVBoxLayout(panel) for panel in self.columns]
        for column in (left, right):
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(12)

        self.todo_card = Section("Todo", 15, "添加 Todo ＋", lambda: self.add_entry("Todo"))
        self.todo_status = label("未完成事项按截止日期排列。", "TodayMuted")
        self.todo_status.setWordWrap(True)
        self.todo_card.body.addWidget(self.todo_status)
        self.todo_items = TodayList("todo")
        self.todo_items.itemDoubleClicked.connect(self.open_todo)
        self.todo_items.addItem("暂无其他待办。添加 Todo 可设置截止日期。")
        self.todo_items.fit(4)
        self.todo_card.body.addWidget(self.todo_items)
        self.task_card = Section("今日要做", 9, "添加任务 ＋", lambda: self.add_entry("今日任务"))
        self.tasks = TodayList("task")
        self.tasks.itemChanged.connect(self.toggle_task)
        self.tasks.itemDoubleClicked.connect(lambda item: self.edit_today())
        self.tasks.setToolTip("点复选框完成任务；选中后按空格也可以勾选。双击打开今日计划。")
        self.task_card.body.addWidget(self.tasks)
        self.daily_hint = label("", "TodayMuted")
        self.daily_hint.setWordWrap(True)
        self.task_card.body.addWidget(self.daily_hint)
        left.addWidget(self.task_card)
        left.addWidget(self.todo_card)
        quick = Section("快捷操作", 14)
        actions = FlowLayout()
        for title, action, color in (
            ("进入项目", lambda: window.navigation.setCurrentRow(window.PROJECT), "blue"),
            ("打开 Agent", lambda: window.navigation.setCurrentRow(window.AGENT), "purple"),
            ("找 Prompt / Skill", lambda: window.navigation.setCurrentRow(window.LIBRARY), "green"),
            ("打开 Obsidian", self.open_daily, "purple")):
            action_button = control(title, action)
            action_button.setProperty("quickColor", color)
            actions.addWidget(action_button)
        quick.body.addLayout(actions)
        left.addWidget(quick)
        left.addStretch()

        self.project_card = Section("需要继续的项目", 3, "全部项目 →", lambda: window.navigation.setCurrentRow(window.PROJECT))
        self.continue_projects = TodayList("project")
        self.continue_projects.itemDoubleClicked.connect(self.open_project)
        self.project_card.body.addWidget(self.continue_projects)
        right.addWidget(self.project_card)
        self.recent_card = Section("最近工作", 13, "查看项目 →", lambda: window.navigation.setCurrentRow(window.PROJECT))
        self.recent_projects = TodayList("recent")
        self.recent_projects.itemDoubleClicked.connect(self.open_project)
        self.recent_card.body.addWidget(self.recent_projects)
        right.addWidget(self.recent_card)
        self.attention_card = Section("需要处理", 11, "数据安全 →", lambda: window.navigation.setCurrentRow(window.SAFETY))
        self.attention = TodayList("alert")
        self.attention.itemDoubleClicked.connect(self.open_attention)
        self.attention_card.body.addWidget(self.attention)
        right.addWidget(self.attention_card)
        protection = Section("资料与备份状态", 12, "查看 →", lambda: window.navigation.setCurrentRow(window.SAFETY))
        self.safety = label("", "TodayMuted")
        self.safety.setWordWrap(True)
        protection.body.addWidget(self.safety)
        self.backup_tiles = QWidget()
        self.backup_layout = QGridLayout(self.backup_tiles)
        self.backup_layout.setContentsMargins(0, 0, 0, 0)
        self.backup_layout.setSpacing(6)
        self.backup_signature = None
        protection.body.addWidget(self.backup_tiles)
        right.addWidget(protection)
        right.addWidget(link("旧版工作笔记", self.legacy_notes), 0, Qt.AlignmentFlag.AlignRight)
        right.addStretch()
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)
        self.arrange_columns()

    def arrange_columns(self):
        narrow, stacked = self.width() < 860, self.width() < 640
        if (narrow, stacked) == self.narrow:
            return
        self.narrow = (narrow, stacked)
        for panel in self.columns:
            self.columns_layout.removeWidget(panel)
        for i, panel in enumerate(self.columns):
            self.columns_layout.addWidget(panel, i if stacked else 0, 0 if stacked else i, Qt.AlignmentFlag.AlignTop)
        self.columns_layout.setColumnStretch(0, 1)
        self.columns_layout.setColumnStretch(1, 0 if stacked else 1)
        for widget in self.metrics:
            self.metric_grid.removeWidget(widget)
        for i, widget in enumerate(self.metrics):
            widget.icon.setVisible(not narrow)
            self.metric_grid.addWidget(widget, i // 2 if stacked else 0, i % 2 if stacked else i)
        visible_columns = 2 if stacked else len(self.metrics)
        for i in range(4):
            self.metric_grid.setColumnStretch(i, 1 if i < visible_columns else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "columns"):
            self.arrange_columns()

    def add_entry(self, section, day=None, parent=None):
        if not self.report:
            return
        expected, daily = self.report["original"], self.daily()
        is_todo = section == "Todo"
        entry_section = "今日任务" if is_todo else section
        dialog = DailyEntryDialog(parent or self, section, day or (date.today() if is_todo else self.day))
        if dialog.exec() == QDialog.DialogCode.Accepted:
            title, clock = dialog.values()
            target_day = dialog.date.date().toPython()
            target = Daily(daily.files.root, target_day)
            def append(context):
                snapshot = expected if target_day == daily.day else target.load()["original"]
                context.checkpoint()
                return target.add_entry(entry_section, title, snapshot, time=clock)
            self.window.submit(None, "添加 Todo" if is_todo else "添加" + ("任务" if section == "今日任务" else "日程"),
                append, lambda _: self.refresh() if is_todo else self.set_day(target_day), persist_result=False)

    def set_day(self, day):
        self.follow_today = day == date.today()
        if day != self.day:
            self.day = day
            self.report = None
            self.tasks.setEnabled(False)
            self.edit_button.setEnabled(False)
            self.tasks.blockSignals(True)
            self.tasks.clear()
            self.tasks.blockSignals(False)
            self.tasks.addItem("正在读取所选日期…")
            self.todo_items.clear()
        self.date_picker.blockSignals(True)
        self.date_picker.setDate(QDate(day.year, day.month, day.day))
        self.date_picker.blockSignals(False)
        self.refresh()

    def open_agenda(self):
        from .agenda import AgendaPanel
        dialog = QDialog(self)
        dialog.setWindowTitle("日程总览 · 任务与安排")
        dialog.resize(920, 620)
        layout = QVBoxLayout(dialog)
        panel = AgendaPanel(self.window, self, dialog)
        layout.addWidget(panel)
        layout.addWidget(control("关闭", dialog.accept))
        panel.refresh()
        try:
            dialog.exec()
        finally:
            panel.stop_updates()
            dialog.deleteLater()

    def daily(self):
        return Daily(work_root(self.window.store), self.day)

    def manual_refresh(self):
        today = date.today()
        if self.follow_today and self.day != today:
            self.set_day(today)
        else:
            self.refresh()

    def refresh(self):
        if self.closed:
            return
        self.loaded_once = True
        self.heading.setText(self.day.strftime("%Y 年 %m 月 %d 日") + " · " + "星期" + "一二三四五六日"[self.day.weekday()])
        is_today = self.day == date.today()
        self.edit_button.setText("编辑今日计划" if is_today else "编辑当日计划")
        self.today_button.setEnabled(not is_today)
        self.task_card.title.setText("今日要做" if is_today else "当日要做")
        self.metrics[0].title.setText("今日要做" if is_today else "当日要做")
        notes_root = work_root(self.window.store)
        self.notes_root = notes_root
        if self.todo_index is None or self.todo_root != notes_root:
            self.todo_index = Agenda(notes_root)
            self.todo_root = notes_root
        todo_index = self.todo_index
        daily, root = Daily(notes_root, self.day), self.window.store.setting("project_workspace", "")
        def read(context):
            report = daily.load()
            todo_report = todo_index.scan(tasks_only=True, context=context)
            context.checkpoint()
            projects, errors = [], list(todo_report["errors"])
            watches = [str(notes_root), str(Path(report["path"]).parent), report["path"], *todo_report["watches"]]
            pending = [row for row in todo_report["items"] if not row["done"] and row["day"] != self.day.isoformat()]
            if root:
                workspace = ProjectWorkspace(Path(root))
                try:
                    listing = workspace.list_projects(context)
                except (UserError, OSError) as exc:
                    listing = {"projects": []}
                    errors.append("项目工作区需要检查：" + str(exc))
                for item in listing["projects"]:
                    if item["state"] == "archived" or item.get("legacy"):
                        continue
                    try:
                        doc = workspace.document(item["id"], "agent/STATUS.md")
                        projects.append({**item, **project_context(doc["text"], item["state"])})
                        watches.append(doc["path"])
                    except (UserError, OSError):
                        errors.append(item["name"] + "：项目状态需要检查。")
                watches += [str(workspace.registry_path), str(workspace.registry_path.parent)]
            return {"daily": report, "projects": projects, "errors": errors, "watches": watches,
                    "todos": pending, "todo_errors": todo_report["errors"]}
        self.read_background(read, self.render)

    def render(self, report):
        if not self.matches_day(report["daily"]):
            return
        self.projects = report["projects"]
        self.render_daily(report["daily"], report.get("todos"), report.get("todo_errors", []))
        self.render_projects(report)

    def render_daily(self, report, todos=None, todo_errors=None):
        if not self.matches_day(report):
            return
        self.report = report
        if todos is not None:
            self.todo_items.clear()
            ordered = sorted(todos, key=lambda row: (row["day"] > date.today().isoformat(), row["day"]))
            for row in ordered[:6]:
                item = QListWidgetItem(row["title"])
                item.setData(ROW_DATA, row)
                item.setToolTip("截止日期：" + row["day"] + "\n" + row["title"])
                self.todo_items.addItem(item)
            if not self.todo_items.count():
                self.todo_items.addItem("暂无其他待办。添加 Todo 可设置截止日期。")
            self.todo_items.fit(6)
            issue_count = len(todo_errors or [])
            self.todo_status.setText(f"{len(todos)} 项待办 · 按截止日期排列，双击到期日处理。" +
                                      (f" 部分文件未读：{issue_count} 项。" if issue_count else ""))
        self.tasks.blockSignals(True)
        self.tasks.clear()
        for row in self.report["tasks"]:
            item = QListWidgetItem(row["text"])
            item.setData(Qt.ItemDataRole.UserRole, row["line"])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if row["done"] else Qt.CheckState.Unchecked)
            item.setData(ROW_DATA, row)
            item.setToolTip(row["text"])
            self.tasks.addItem(item)
        self.tasks.blockSignals(False)
        if not self.tasks.count():
            item = QListWidgetItem("这一天还没有安排。点“添加任务”开始。")
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tasks.addItem(item)
        self.tasks.fit(6)
        count = len(report["tasks"])
        done = sum(row["done"] for row in report["tasks"])
        self.task_card.title.setText(("今日要做" if self.day == date.today() else "当日要做") + f"  ({done}/{count})")
        self.daily_hint.setText("勾选即保存 · 与 Todo 共用每日 Markdown")
        self.metrics[0].value.setText(f"{done} / {count}")
        self.metrics[0].progress.setValue(round(done * 100 / count) if count else 0)
        self.metrics[0].note.setText(f"已完成 {round(done * 100 / count)}%" if count else "添加第一项任务")
        self.edit_button.setEnabled(True)
        if not self.window.jobs:
            self.tasks.setEnabled(True)

    def matches_day(self, report):
        return report["day"] == self.day.isoformat() and Path(report["path"]) == self.daily().files.path(self.daily().relative)

    def render_projects(self, report):
        self.continue_projects.clear()
        self.project_errors = report["errors"]
        active = []
        for row in self.projects:
            if row["state"] in {"completed", "paused"}:
                continue
            active.append(row)
            step = row["next_step"]
            preview = step if len(step) <= 100 else step[:99] + "…"
            item = QListWidgetItem(row["name"] + " · " + row["phase"] + "\n下一步：" + preview)
            item.setToolTip(row["name"] + "\n" + step)
            item.setData(Qt.ItemDataRole.UserRole, row["id"])
            item.setData(ROW_DATA, {**row, "position": len(active) - 1})
            self.continue_projects.addItem(item)
        if not self.continue_projects.count():
            self.continue_projects.addItem("暂无待继续项目。在“项目”中选择工作区或新建项目。")
        self.continue_projects.fit(4)
        self.metrics[2].value.setText(str(len(active)) + " 个")
        self.metrics[2].note.setText("双击卡片继续工作" if active else "选择工作区或创建项目")
        recent = sorted(self.projects, key=lambda p: (self.window.store.evidence("project-used:" + p["id"]) or {}).get("at", ""), reverse=True)
        self.recent_projects.clear()
        for row in recent[:4]:
            used = self.window.store.evidence("project-used:" + row["id"])
            if used:
                item = QListWidgetItem(row["name"] + " · " + readable_time(used["at"]))
                item.setData(Qt.ItemDataRole.UserRole, row["id"])
                item.setData(ROW_DATA, {"title": row["name"], "at": readable_time(used["at"])})
                item.setToolTip(row["name"] + " · 最近进入：" + readable_time(used["at"]))
                self.recent_projects.addItem(item)
        if not self.recent_projects.count():
            self.recent_projects.addItem("进入项目后，这里会显示最近使用。")
        self.recent_projects.fit(3)
        self.refresh_attention(report["errors"])
        self.watch(report["watches"])

    def refresh_attention(self, errors=None):
        self.attention.clear()
        for value in (self.project_errors if errors is None else errors):
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
        count = self.attention.count()
        for i in range(count):
            item = self.attention.item(i)
            item.setData(ROW_DATA, {"title": item.text()})
            item.setToolTip(item.text())
        if not count:
            self.attention.addItem("目前没有需要处理的异常。")
        self.attention.fit(3)
        self.metrics[1].value.setText(str(count) + " 项")
        self.metrics[1].note.setText("双击查看具体事项" if count else "暂无待处理异常")
        self.attention_card.title.setText("需要处理" + (f"  ({count})" if count else ""))
        health = getattr(self.window, "dashboard_health", [])
        rows = [(r, h) for r, h in health if r.kind in {"hermes_local", "agent"}]
        signature = [(r.id, r.name, h.get("state"), h.get("created_at")) for r, h in rows[:4]]
        self.safety.setText("显示已记录的备份结果，运行状态需按需检查。" if rows else "重要资料的备份与恢复在“数据安全”。")
        if signature != self.backup_signature:
            self.backup_signature = signature
            while self.backup_layout.count():
                widget = self.backup_layout.takeAt(0).widget()
                if widget:
                    widget.deleteLater()
            for i, (resource, status) in enumerate(rows[:4]):
                tile = BackupButton(resource.name, status.get("state", "待检查") + " · " + (readable_time(status["created_at"]) if status.get("created_at") else "暂无备份"))
                tile.clicked.connect(lambda checked=False, resource=resource: self.window.open_safety_resource(resource))
                self.backup_layout.addWidget(tile, i // 2, i % 2)

    def edit_today(self):
        if not self.report:
            return
        daily, expected = self.daily(), self.report["original"]
        text = edit_markdown(
            self, self.day.isoformat() + " · 每日计划",
            self.report["text"],
            "“今日要做”和截止日期为这一天的 Todo 共用此 Markdown；保存后会刷新两块清单。",
        )
        if text is not None:
            self.window.submit(None, "保存当日计划", lambda context: daily.save(text, expected), lambda _: self.refresh(), persist_result=False)

    def toggle_task(self, item):
        if not self.report or item.data(Qt.ItemDataRole.UserRole) is None:
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

    def open_todo(self, item):
        row = item.data(ROW_DATA) or {}
        if row.get("day"):
            self.set_day(date.fromisoformat(row["day"]))

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
        self.source_button = control("打开来源", self.open_source)
        self.local_button = control("查看本地目录", self.open_local)
        actions.addWidget(self.source_button)
        actions.addWidget(self.local_button)
        actions.addWidget(control("显示全部资源", self.clear_scope))
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
        preferred = getattr(self, "preferred_path", "")
        preferred_item = None
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
            if entry["path"] == preferred:
                preferred_item = cell
        if preferred_item is not None:
            self.listing.setCurrentItem(preferred_item)
            self.preferred_path = ""
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
        for widget in (self.edit_button, self.favorite_button, self.obsidian_button, self.local_button):
            widget.setEnabled(item is not None)
        self.source_button.setEnabled(bool(item and item["metadata"].get("source")))
        if not item:
            self.preview.setPlainText("还没有匹配资源。点击“收藏新资源”，或将带 frontmatter 的 Markdown 放入工作台的“资源”文件夹。")
            return
        metadata = item["metadata"]
        status = " · ".join(label + ("：是" if metadata.get(key) else "：否") for key, label in (("configured", "已配置"), ("tested", "已测试")))
        text = "# " + item["name"] + "\n\n" + str(metadata.get("summary", "")) + "\n\n" + status + "\n\n" + item["body"]
        if metadata.get("notes"):
            text += "\n\n## 我的备注\n\n" + str(metadata["notes"])
        self.preview.setMarkdown(text)

    def edit_item(self, item, on_saved=None):
        dialog = CatalogDialog(self, item)
        if item is None:
            dialog.fields["projects"].setText(self.project_filter)
            dialog.fields["agents"].setText(self.agent_filter)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        metadata, body = dialog.metadata(), dialog.body.toPlainText()
        catalog = Catalog(work_root(self.window.store))
        if item is None:
            self.search.clear()
            self.kind.setCurrentIndex(0)
            self.favorite.setChecked(False)
        def saved(report):
            self.preferred_path = report["path"]
            self.refresh()
            if on_saved:
                on_saved()
        self.window.submit(None, "保存资源 Markdown", lambda context: catalog.save(metadata, body, item), saved, persist_result=False)

    def toggle_favorite(self):
        item, catalog = self.selected(), self.catalog
        if item:
            self.window.submit(None, "更新资源收藏", lambda context: catalog.save({"favorite": not item["metadata"].get("favorite")}, item["body"], item), lambda _: self.refresh(), persist_result=False)

    def safe_url(self, url):
        if url.scheme() not in {"https", "http"} or not url.host() or url.userName() or url.password():
            QMessageBox.information(self, "打开来源", "只支持不含登录凭据的 http / https 链接。")
            return
        if not QDesktopServices.openUrl(url):
            QMessageBox.information(self, "打开来源", "系统未能打开链接，请检查默认浏览器设置。")

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

    def set_scope(self, project="", agent="", related=()):
        self.project_filter, self.agent_filter, self.related_ids = project, agent, list(related)
        self.search.clear()
        self.kind.setCurrentIndex(0)
        self.favorite.setChecked(False)
        self.render()

    def clear_scope(self):
        self.set_scope()


class AgentPage(QWidget):
    """Daily entry points for the same registered objects used by existing adapters."""
    def __init__(self, window):
        super().__init__()
        self.setObjectName("AgentPage")
        self.window = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        heading_row = QHBoxLayout()
        heading = QLabel("Agent")
        heading.setObjectName("Title")
        heading_row.addWidget(heading, 1)
        self.register_agent_button = control("＋ 登记 Agent", lambda: window.add_resource("agent"), True)
        self.register_local_button = control("登记本地 Hermes", lambda: window.add_resource("hermes_local"))
        self.register_server_button = control("登记服务器 Hermes", lambda: window.add_resource("hermes_server"))
        self.refresh_button = control("刷新", self.refresh)
        for widget in (self.register_agent_button, self.register_local_button, self.register_server_button, self.refresh_button):
            heading_row.addWidget(widget)
        layout.addLayout(heading_row)
        note = QLabel("打开常用入口，查看记录和相关资源。Hermes 的网关与恢复管理保留在数据安全中。")
        note.setWordWrap(True)
        note.setObjectName("Subtitle")
        layout.addWidget(note)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        list_heading = QHBoxLayout()
        self.list_title = QLabel("Agent 列表")
        self.list_title.setObjectName("SectionTitle")
        self.agent_count = QLabel("0 个")
        self.agent_count.setObjectName("Subtitle")
        list_heading.addWidget(self.list_title)
        list_heading.addStretch(1)
        list_heading.addWidget(self.agent_count)
        left_layout.addLayout(list_heading)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索 Agent 名称或类型…")
        self.search.textChanged.connect(self.render_list)
        left_layout.addWidget(self.search)
        self.listing = QListWidget()
        self.listing.setObjectName("AgentListing")
        self.listing.setSpacing(5)
        self.listing.setMinimumWidth(250)
        self.listing.currentItemChanged.connect(self.render)
        left_layout.addWidget(self.listing, 1)
        split.addWidget(left)
        panel = QWidget()
        panel.setObjectName("AgentDetail")
        pane = QVBoxLayout(panel)
        pane.setContentsMargins(0, 0, 0, 0)
        pane.setSpacing(10)
        self.hero = QFrame()
        self.hero.setObjectName("AgentHero")
        hero_layout = QVBoxLayout(self.hero)
        hero_layout.setContentsMargins(18, 16, 18, 14)
        hero_top = QHBoxLayout()
        self.agent_icon = QLabel("A")
        self.agent_icon.setObjectName("AgentIcon")
        self.agent_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.agent_icon.setFixedSize(62, 62)
        hero_top.addWidget(self.agent_icon)
        hero_title_col = QVBoxLayout()
        self.agent_name = QLabel("选择一个 Agent")
        self.agent_name.setObjectName("AgentName")
        self.agent_kind = QLabel("登记后，这里会显示入口、记录和备份状态。")
        self.agent_kind.setObjectName("Subtitle")
        hero_title_col.addWidget(self.agent_name)
        hero_title_col.addWidget(self.agent_kind)
        hero_top.addLayout(hero_title_col, 1)
        hero_layout.addLayout(hero_top)
        self.agent_meta = QLabel("")
        self.agent_meta.setObjectName("AgentMeta")
        self.agent_meta.setWordWrap(True)
        hero_layout.addWidget(self.agent_meta)
        path_row = QHBoxLayout()
        self.agent_path = QLabel("")
        self.agent_path.setObjectName("AgentPath")
        self.agent_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.agent_path.setWordWrap(True)
        path_row.addWidget(self.agent_path, 1)
        self.copy_path_button = control("复制路径", self.copy_path)
        self.copy_path_button.setEnabled(False)
        path_row.addWidget(self.copy_path_button)
        hero_layout.addLayout(path_row)
        pane.addWidget(self.hero)

        row = FlowLayout()
        self.actions = []
        for text, action, primary in (("打开常用入口", self.open_agent, True), ("查看记录", self.records, False),
                             ("相关资源", self.resources, False), ("写备注", self.notes, False), ("编辑登记", self.edit, False), ("查看目录", self.directory, False), ("备份与恢复", self.safety, False)):
            widget = control(text, action, primary)
            self.actions.append(widget)
            row.addWidget(widget)
        pane.addLayout(row)

        self.metrics = [Metric(title, color, icon, "请选择 Agent") for title, color, icon in (
            ("记录目录", COLORS[0], 10), ("相关资源", COLORS[3], 6),
            ("最近操作", COLORS[2], 13), ("备份状态", COLORS[1], 3))]
        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(8)
        for metric in self.metrics:
            metrics_row.addWidget(metric, 1)
        pane.addLayout(metrics_row)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_agent_overview(), "概览")
        self.tabs.addTab(self.build_activity_tab(), "最近记录")
        self.tabs.addTab(self.build_resources_tab(), "相关资源")
        self.tabs.addTab(self.build_notes_tab(), "备注")
        self.tabs.addTab(self.build_backup_tab(), "备份状态")
        pane.addWidget(self.tabs, 1)
        split.addWidget(panel)
        split.setSizes([300, 930])
        layout.addWidget(split, 1)
        self.resources_data = []
        self.catalog_items = []

    def _section(self, title, icon, action=None, callback=None):
        section = Section(title, icon, action, callback)
        section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return section

    def build_agent_overview(self):
        page = QWidget()
        grid = QGridLayout(page)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        notes = self._section("备注", 13, "编辑", self.notes)
        self.notes_preview = QLabel("暂无备注。可以记录使用习惯、配置说明或换电脑提醒。")
        self.notes_preview.setWordWrap(True)
        self.notes_preview.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        notes.body.addWidget(self.notes_preview, 1)
        recent = self._section("最近操作", 13, "查看全部", lambda: self.show_tab(1))
        self.recent_preview = QListWidget()
        self.recent_preview.setObjectName("AgentCompactList")
        recent.body.addWidget(self.recent_preview, 1)
        related = self._section("相关资源", 6, "查看全部", self.resources)
        self.related_preview = QListWidget()
        self.related_preview.setObjectName("AgentCompactList")
        self.related_preview.itemDoubleClicked.connect(lambda *_: self.resources())
        related.body.addWidget(self.related_preview, 1)
        backup = self._section("备份状态", 3, "打开备份与恢复", self.safety)
        self.backup_preview = QLabel("选择 Agent 查看已记录的备份状态。")
        self.backup_preview.setWordWrap(True)
        backup.body.addWidget(self.backup_preview, 1)
        grid.addWidget(notes, 0, 0)
        grid.addWidget(recent, 0, 1)
        grid.addWidget(related, 1, 0)
        grid.addWidget(backup, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 1)
        return page

    def build_activity_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        top.addWidget(QLabel("管家在此 Agent 上执行过的备份、检查或目录操作；不读取 Agent 私有对话。"), 1)
        top.addWidget(control("打开完整操作记录", lambda: self.window.show_resource_activity(self.selected()) if self.selected() else None))
        layout.addLayout(top)
        self.activity_list = QListWidget()
        self.activity_list.setObjectName("AgentCompactList")
        self.activity_list.itemDoubleClicked.connect(lambda *_: self.window.show_resource_activity(self.selected()) if self.selected() else None)
        layout.addWidget(self.activity_list, 1)
        return page

    def build_resources_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("来自资源库中适用 Agent 与该类型的条目。"), 1)
        row.addWidget(control("在资源库中打开", self.resources))
        layout.addLayout(row)
        self.resource_list = QListWidget()
        self.resource_list.setObjectName("AgentCompactList")
        self.resource_list.itemDoubleClicked.connect(lambda *_: self.resources())
        layout.addWidget(self.resource_list, 1)
        return page

    def build_notes_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("备注与使用说明保存在资源库 Markdown 中。"), 1)
        row.addWidget(control("编辑备注", self.notes))
        layout.addLayout(row)
        self.notes_detail = QTextBrowser()
        self.notes_detail.setOpenExternalLinks(False)
        layout.addWidget(self.notes_detail, 1)
        return page

    def build_backup_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("备份与恢复继续使用现有数据安全流程；这里只展示状态并提供入口。"))
        self.backup_detail = QTextBrowser()
        self.backup_detail.setOpenExternalLinks(False)
        layout.addWidget(self.backup_detail, 1)
        layout.addWidget(control("进入数据安全中的此 Agent", self.safety), alignment=Qt.AlignmentFlag.AlignLeft)
        return page

    def selected(self):
        item = self.listing.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def refresh(self, selected_id=None):
        previous = self.selected()
        identity = selected_id or (previous.id if previous else "")
        self.resources_data = [r for r in self.window.store.resources() if r.kind in {"agent", "hermes_local", "hermes_server"}]
        self.render_list(identity)
        self.catalog_items = list(getattr(self.window.catalog_page, "items", []))
        self.render()
        if not self.catalog_items:
            catalog = Catalog(work_root(self.window.store))
            def loaded(report):
                self.catalog_items = report["items"]
                self.render()
            self.window.submit(None, "读取 Agent 关联资源", catalog.scan, loaded, persist_result=False)

    def render_list(self, selected_id=""):
        previous = self.selected()
        identity = selected_id or (previous.id if previous else "")
        query = self.search.text().strip().casefold()
        self.listing.blockSignals(True)
        self.listing.clear()
        shown = 0
        for resource in self.resources_data:
            engine = resource.options.get("engine", "服务器 Hermes" if resource.kind == "hermes_server" else "Hermes")
            if query and query not in (resource.name + " " + engine).casefold():
                continue
            item = QListWidgetItem(resource.name + "\n" + engine + "   ● 已登记")
            item.setSizeHint(QSize(240, 62))
            item.setToolTip(resource.name + "\n" + engine)
            item.setData(Qt.ItemDataRole.UserRole, resource)
            self.listing.addItem(item)
            shown += 1
            if resource.id == identity:
                self.listing.setCurrentItem(item)
        if self.listing.currentRow() < 0 and self.listing.count():
            self.listing.setCurrentRow(0)
        self.listing.blockSignals(False)
        self.agent_count.setText(f"{shown} 个")
        self.list_title.setText("Agent 列表")
        self.render()

    def render(self, *_):
        resource = self.selected()
        for action in self.actions:
            action.setEnabled(resource is not None)
        if not resource:
            self.agent_name.setText("选择一个 Agent")
            self.agent_kind.setText("登记后，这里会显示入口、记录和备份状态。")
            self.agent_meta.clear()
            self.agent_path.clear()
            self.copy_path_button.setEnabled(False)
            self.notes_preview.setText("暂无备注。")
            self.notes_detail.setPlainText("选择一个 Agent 查看备注。")
            self.recent_preview.clear(); self.activity_list.clear()
            self.related_preview.clear(); self.resource_list.clear()
            self.backup_preview.setText("选择 Agent 查看已记录的备份状态。")
            self.backup_detail.clear()
            for metric in self.metrics:
                metric.value.setText("—"); metric.note.setText("请选择 Agent")
            return
        used = self.window.store.evidence("agent-used:" + resource.id) or {}
        health = next((h for r, h in getattr(self.window, "dashboard_health", []) if r.id == resource.id), {})
        options = resource.options
        engine = options.get("engine", "服务器 Hermes" if resource.kind == "hermes_server" else "Hermes")
        self.agent_name.setText(resource.name)
        self.agent_icon.setText("H" if "hermes" in resource.kind else engine[:1].upper())
        self.agent_kind.setText("● 已登记  ·  " + engine)
        self.agent_meta.setText("最近使用：" + (readable_time(used["at"]) if used else "尚未从管家打开") +
                                "     ·     最近备份：" + readable_time(health.get("created_at") or "尚未备份"))
        data_dir = options.get("home") or options.get("config_dir") or options.get("path") or (options.get("record_paths") or [""])[0]
        self.agent_path.setText("数据目录：" + (str(data_dir) if data_dir else "未设置"))
        self.copy_path_button.setEnabled(bool(data_dir))
        self.current_path = str(data_dir)
        self.current_health = health
        self.current_engine = engine
        note = str(options.get("notes", ""))
        agent_note = next((entry for entry in self.catalog_items
                           if entry["type"] == "agent" and resource.id in entry["metadata"].get("agents", [])), None)
        if agent_note:
            note = agent_note["body"] or str(agent_note["metadata"].get("notes", ""))
        if not note:
            note = "暂无备注。可以记录使用习惯、配置说明或换电脑提醒。"
        self.notes_preview.setText(note or "暂无备注。可以记录使用习惯、配置说明或换电脑提醒。")
        self.notes_detail.setMarkdown("# " + resource.name + " · 备注\n\n" + note)

        record_paths = options.get("record_paths", [])
        if isinstance(record_paths, str):
            record_paths = [record_paths] if record_paths else []
        existing_records = sum(Path(path).is_dir() for path in record_paths)
        related = self.related_items(resource)
        tasks = self.window.store.task_summaries(limit=8, resource_id=resource.id)
        values = (str(len(record_paths)), str(len(related)), str(len(tasks)), health.get("state", "未检测"))
        metric_notes = (f"{existing_records} 个目录可用", "适用于此 Agent 的资料", "管家操作，不含聊天内容",
                        "最近备份：" + readable_time(health.get("created_at") or "暂无"))
        for metric, value, hint in zip(self.metrics, values, metric_notes):
            metric.value.setText(value)
            metric.note.setText(hint)
        self.recent_preview.clear(); self.activity_list.clear()
        if tasks:
            for task in tasks:
                stamp = readable_time(task.get("finished_at") or task.get("started_at") or "")
                title = f"{task['title']}  ·  {stamp}  ·  {task['state']}"
                self.recent_preview.addItem(title)
                self.activity_list.addItem(title)
        else:
            self.recent_preview.addItem("暂无管家操作记录。")
            self.activity_list.addItem("暂无管家操作记录。")
        self.related_preview.clear(); self.resource_list.clear()
        for entry in related:
            title = entry["name"] + "  ·  " + TYPES.get(entry["type"], entry["type"])
            self.related_preview.addItem(title)
            self.resource_list.addItem(title)
        if not related:
            self.related_preview.addItem("暂无关联资源。")
            self.resource_list.addItem("暂无关联资源。可从资源库关联。")
        backup_text = ("备份状态：" + health.get("state", "未检测") + "\n\n最近备份：" +
                       readable_time(health.get("created_at") or "尚未备份") +
                       "\n完整性校验：" + ("已校验" if health.get("verified") else "尚未验证") +
                       "\n恢复演练：" + ("已完成" if health.get("rehearsed") else "尚未演练"))
        self.backup_preview.setText(backup_text)
        self.backup_detail.setPlainText(backup_text + "\n\n备份和恢复操作仍由“数据安全”模块执行。")

    def related_items(self, resource):
        catalog = self.catalog_items
        refs = {resource.id.casefold(), self.current_engine.casefold() if hasattr(self, "current_engine") else resource.options.get("engine", "").casefold()}
        return [entry for entry in catalog if refs & {str(value).casefold() for value in entry["metadata"].get("agents", [])}]

    def show_tab(self, index):
        self.tabs.setCurrentIndex(index)

    def copy_path(self):
        if getattr(self, "current_path", ""):
            QApplication.clipboard().setText(self.current_path)
            self.window.statusBar().showMessage("路径已复制", 2500)

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
            else:
                QMessageBox.information(self, "常用入口", "系统未能打开链接，请检查默认浏览器设置。")
        elif resource.options.get("executable") and resource.kind != "hermes_server":
            self.window.perform(resource, "start")
            self.used(resource)
        elif resource.kind == "hermes_server":
            self.safety()
        else:
            value = self.local_directory(resource)
            if value:
                if self.window.open_path(Path(value)):
                    self.used(resource)

    def records(self):
        resource = self.selected()
        if resource:
            self.window.perform(resource, "records" if resource.kind == "agent" else "logs" if resource.kind == "hermes_server" else "library")

    def resources(self):
        resource = self.selected()
        if resource:
            self.window.catalog_page.set_scope(agent=resource.id)
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
            page.catalog = catalog
            item = next((i for i in report["items"] if i["type"] == "agent" and resource.id in i["metadata"].get("agents", [])), None)
            if item:
                page.edit_item(item, self.resources)
            else:
                dialog = CatalogDialog(self)
                dialog.fields["name"].setText(resource.name + " · 使用说明与备注")
                dialog.kind.setCurrentIndex(dialog.kind.findData("agent"))
                dialog.fields["agents"].setText(resource.id)
                if dialog.exec() == QDialog.DialogCode.Accepted:
                    metadata, body = dialog.metadata(), dialog.body.toPlainText()
                    def saved(report):
                        page.preferred_path = report["path"]
                        self.resources()
                    self.window.submit(None, "保存 Agent 备注 Markdown", lambda context: catalog.save(metadata, body), saved, persist_result=False)
        self.window.submit(None, "定位 Agent 备注", catalog.scan, loaded, persist_result=False)

    def safety(self):
        if self.selected():
            self.window.open_safety_resource(self.selected())
