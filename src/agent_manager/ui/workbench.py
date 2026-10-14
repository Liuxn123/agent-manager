from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QFileSystemWatcher, QEvent, QUrl, QDate, QSize, QProcess, QRect, QFileInfo
from PySide6.QtGui import QDesktopServices, QColor, QPainter, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QApplication, QFrame,
    QTextBrowser, QTextEdit, QSplitter, QListWidget, QListWidgetItem, QDialog,
    QDialogButtonBox, QFormLayout, QLineEdit, QComboBox, QCheckBox, QScrollArea, QMessageBox,
    QGridLayout, QDateEdit, QTabWidget, QSizePolicy, QFileDialog, QTabBar, QFileIconProvider,
    QProgressBar, QStyledItemDelegate, QStyle, QToolButton, QMenu)

from ..domain import UserError
from ..maintenance import backup_health
from ..planbook import PlanBook, occurs_on
from ..project_workspaces import ProjectWorkspace
from ..runtime import TaskContext
from ..storage import now
from ..workbench import Daily, Catalog, TYPES, work_root, markdown_uri, project_context
from .components import FlowLayout, nav_icon
from .tasks import ReadWorker
from .presentation import readable_time
from .today_widgets import Metric, Section, TodayList, ROW_DATA, COLORS, link, label
from .task_dialog import UnifiedTaskDialog


def control(text, action, primary=False):
    widget = QPushButton(text)
    widget.setProperty("primary", primary)
    widget.clicked.connect(action)
    return widget


class CatalogResourceDelegate(QStyledItemDelegate):
    """Compact table-like resource rows matching the library prototype."""
    COLORS = {"article": "#18a9c9", "github": "#3979ff", "template": "#8b55e8",
              "prompt": "#8b55e8", "tool": "#2778ee", "website": "#f09b24",
              "skill": "#18a77b", "mcp": "#18a77b", "api": "#e45b87", "agent": "#596b88",
              "other": "#8495ad"}
    ICONS = {"article": "▤", "github": "</>", "template": "▣", "prompt": "✦",
             "tool": "⚒", "website": "◎", "skill": "⬡", "mcp": "↔", "api": "⇄", "agent": "♙"}

    def sizeHint(self, option, index):
        return QSize(440, 55)

    def paint(self, painter, option, index):
        entry = index.data(Qt.ItemDataRole.UserRole) or {}
        metadata = entry.get("metadata", {})
        kind = entry.get("type", "other")
        rect = option.rect.adjusted(2, 1, -2, -1)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setBrush(QColor("#edf5ff"))
            painter.drawRoundedRect(rect, 6, 6)
        else:
            painter.setBrush(QColor("#ffffff"))
            painter.drawRect(rect)
        painter.setPen(QColor("#edf1f7"))
        painter.drawLine(rect.left() + 12, rect.bottom(), rect.right() - 8, rect.bottom())
        tile = QRect(rect.left() + 12, rect.top() + 9, 34, 34)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.COLORS.get(kind, "#8495ad")))
        painter.drawRoundedRect(tile, 8, 8)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(tile, Qt.AlignmentFlag.AlignCenter, self.ICONS.get(kind, "◇"))
        width = rect.width()
        name_x = rect.left() + 56
        type_x = rect.left() + int(width * .51)
        tags_x = rect.left() + int(width * .63)
        date_x = rect.left() + int(width * .86)
        name_width = max(60, type_x - name_x - 10)
        font = QFont(painter.font())
        font.setPixelSize(12)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#1c2b48"))
        name = painter.fontMetrics().elidedText(str(entry.get("name", "")), Qt.TextElideMode.ElideRight, name_width)
        painter.drawText(QRect(name_x, rect.top() + 7, name_width, 19), Qt.AlignmentFlag.AlignVCenter, name)
        font.setPixelSize(10)
        font.setBold(False)
        painter.setFont(font)
        summary = str(metadata.get("summary", "") or "").replace("\n", " ")
        detail = summary or "、".join(str(tag) for tag in metadata.get("tags", [])[:3])
        detail = painter.fontMetrics().elidedText(detail, Qt.TextElideMode.ElideRight, name_width)
        painter.setPen(QColor("#8290a7"))
        painter.drawText(QRect(name_x, rect.top() + 28, name_width, 16), Qt.AlignmentFlag.AlignVCenter, detail)
        painter.setPen(QColor(self.COLORS.get(kind, "#8495ad")))
        type_text = painter.fontMetrics().elidedText(TYPES.get(kind, kind), Qt.TextElideMode.ElideRight, max(35, tags_x - type_x - 12))
        painter.drawText(QRect(type_x, rect.top(), max(35, tags_x - type_x - 12), rect.height()), Qt.AlignmentFlag.AlignVCenter, type_text)
        tags = " · ".join(str(tag) for tag in metadata.get("tags", [])[:3])
        painter.setPen(QColor("#607493"))
        tags = painter.fontMetrics().elidedText(tags, Qt.TextElideMode.ElideRight, max(40, date_x - tags_x - 12))
        painter.drawText(QRect(tags_x, rect.top(), max(40, date_x - tags_x - 12), rect.height()), Qt.AlignmentFlag.AlignVCenter, tags)
        painter.setPen(QColor("#71809e"))
        updated = str(metadata.get("updated", "未注明"))[:10]
        painter.drawText(QRect(date_x, rect.top(), max(45, rect.right() - date_x - 34), rect.height()), Qt.AlignmentFlag.AlignVCenter, updated)
        painter.setPen(QColor("#f1a52c" if metadata.get("favorite") else "#aab4c5"))
        painter.drawText(QRect(rect.right() - 27, rect.top(), 22, rect.height()), Qt.AlignmentFlag.AlignCenter, "★" if metadata.get("favorite") else "☆")
        painter.restore()


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
        self.agent_rows = []
        self.narrow = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        top = QHBoxLayout()
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        self.heading = label("工作台", "TodayDate")
        self.heading.setWordWrap(True)
        title_block.addWidget(self.heading)
        self.subtitle = label("今天要做什么、项目进行到哪，一眼掌握。", "TodayMuted")
        title_block.addWidget(self.subtitle)
        top.addLayout(title_block, 1)
        self.open_daily_button = control("打开每日记录", self.open_daily)
        self.open_daily_button.setEnabled(False)
        top.addWidget(self.open_daily_button)
        self.add_task_button = control("＋ 添加任务", self.add_current_entry, True)
        top.addWidget(self.add_task_button)
        self.refresh_button = control("刷新", self.manual_refresh)
        self.refresh_button.setToolTip("刷新今日任务、项目状态与提醒")
        top.addWidget(self.refresh_button)
        layout.addLayout(top)
        note = QHBoxLayout()
        self.date_label = label("", "TodayMuted")
        note.addWidget(self.date_label)
        note.addSpacing(14)
        self.previous_day_button = control("‹", lambda: self.set_day(self.day - timedelta(days=1)))
        note.addWidget(self.previous_day_button)
        self.date_picker = QDateEdit(QDate.currentDate())
        self.date_picker.setCalendarPopup(True)
        self.date_picker.setDisplayFormat("yyyy-MM-dd")
        self.date_picker.setToolTip("选择日期，查看或安排过去与未来的任务。")
        self.date_picker.dateChanged.connect(lambda value: self.set_day(value.toPython()))
        note.addWidget(self.date_picker)
        self.next_day_button = control("›", lambda: self.set_day(self.day + timedelta(days=1)))
        note.addWidget(self.next_day_button)
        self.today_button = control("回到今天", lambda: self.set_day(date.today()))
        note.addWidget(self.today_button)
        note.addStretch()
        layout.addLayout(note)
        self.metric_grid = QGridLayout()
        self.metric_grid.setSpacing(12)
        self.metrics = [Metric(title, color, icon, hint) for title, color, icon, hint in zip(
            ("今日任务", "进行中项目", "运行中的 Agent", "今日提醒"),
            (COLORS[0], COLORS[3], "#22a66b", COLORS[2]), (9, 3, 4, 11),
            ("完成进度", "查看下一步", "本机进程状态", "计划与到期事项"))]
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
        self.columns_layout.setColumnStretch(0, 7)
        self.columns_layout.setColumnStretch(1, 5)

        self.task_card = Section("今日任务", 9)
        self.tasks = TodayList("task")
        self.tasks.itemChanged.connect(self.toggle_task)
        self.tasks.itemDoubleClicked.connect(self.open_task_item)
        self.tasks.setToolTip("点复选框完成任务；选中后按空格也可以勾选。双击打开当日计划。")
        self.daily_hint = label("", "TodayMuted")
        self.daily_hint.setWordWrap(True)
        self.daily_hint.setMaximumHeight(36)

        task_panel = QWidget()
        task_panel_layout = QVBoxLayout(task_panel)
        task_panel_layout.setContentsMargins(0, 0, 0, 0)
        task_panel_layout.setSpacing(6)
        task_panel_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        task_panel_layout.addWidget(self.tasks)
        task_panel_layout.addWidget(self.daily_hint)
        self.task_card.body.addWidget(task_panel)

        self.top_side = QWidget()
        side_layout = QVBoxLayout(self.top_side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(10)
        self.attention_card = Section("今日提醒", 11, "查看计划 →", self.open_plan_records)
        self.attention = TodayList("alert")
        self.attention.itemDoubleClicked.connect(self.open_attention)
        self.attention_card.body.addWidget(self.attention)
        side_layout.addWidget(self.attention_card, 1)

        self.quick_card = Section("工作空间", 8)
        actions = QGridLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setHorizontalSpacing(7)
        actions.setVerticalSpacing(7)
        self.module_buttons = []
        self.module_specs = (
            ("计划管理", "今日待办与周月总结", window.PLAN, "blue"),
            ("项目", "项目进展与资料", window.PROJECT, "purple"),
            ("知识库", "文件、链接、笔记与标签", window.LIBRARY, "green"),
            ("Agent", "登记、Skills 与状态", window.AGENT, "purple"),
            ("电脑与环境", "开发工具与环境变量", window.ENVIRONMENT, "blue"),
            ("数据安全", "本机文件变更追踪", window.SAFETY, "green"),
            ("设置", "外观与工作区偏好", window.SETTINGS, "purple"),
        )
        module_icons = (8, 3, 6, 4, 2, 5, 7)
        for index, (title, detail, page, color) in enumerate(self.module_specs):
            action_button = control(title + "\n" + detail,
                                    lambda _=False, target=page: window.navigation.setCurrentRow(target))
            action_button.setIcon(nav_icon(module_icons[index], COLORS[index % len(COLORS)]))
            action_button.setIconSize(QSize(18, 18))
            action_button.setProperty("quickColor", color)
            action_button.setObjectName("TodayQuickAction")
            action_button.setMinimumHeight(48)
            action_button.setCursor(Qt.CursorShape.PointingHandCursor)
            action_button.setToolTip(detail)
            actions.addWidget(action_button, index // 2, index % 2)
            self.module_buttons.append(action_button)
        self.quick_card.body.addLayout(actions)
        side_layout.addWidget(self.quick_card, 2)

        self.project_card = Section("继续项目", 3, "查看全部 →", lambda: window.navigation.setCurrentRow(window.PROJECT))
        self.continue_projects = TodayList("project")
        self.continue_projects.itemDoubleClicked.connect(self.open_project)
        self.project_card.body.addWidget(self.continue_projects)

        self.agent_card = Section("Agent 状态", 4, "管理 Agent →", lambda: window.navigation.setCurrentRow(window.AGENT))
        self.agent_items = TodayList("agent")
        self.agent_items.itemDoubleClicked.connect(self.open_agent)
        self.agent_card.body.addWidget(self.agent_items)
        self.agent_status_hint = label("正在读取已登记 Agent 的运行状态。", "TodayMuted")
        self.agent_status_hint.setWordWrap(True)
        self.agent_card.body.addWidget(self.agent_status_hint)

        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)
        self.arrange_columns()

    def arrange_columns(self):
        narrow = self.width() < 980
        balanced = self.width() < 1320
        two_metrics = self.width() < 1040
        if (narrow == self.narrow and getattr(self, "balanced_columns", None) == balanced
                and getattr(self, "two_metrics", None) == two_metrics):
            return
        self.narrow, self.balanced_columns, self.two_metrics = narrow, balanced, two_metrics
        for widget in (self.task_card, self.top_side, self.project_card, self.agent_card):
            self.columns_layout.removeWidget(widget)
        for column in range(12):
            self.columns_layout.setColumnStretch(column, 1)
        if narrow:
            self.columns_layout.addWidget(self.task_card, 0, 0, 1, 12)
            self.columns_layout.addWidget(self.top_side, 1, 0, 1, 12)
        else:
            task_columns = 6 if balanced else 7
            side_columns = 12 - task_columns
            self.columns_layout.addWidget(self.task_card, 0, 0, 1, task_columns)
            self.columns_layout.addWidget(self.top_side, 0, task_columns, 1, side_columns)
        if narrow:
            self.columns_layout.addWidget(self.project_card, 2, 0, 1, 12)
            self.columns_layout.addWidget(self.agent_card, 3, 0, 1, 12)
        else:
            self.columns_layout.addWidget(self.project_card, 1, 0, 1, 6)
            self.columns_layout.addWidget(self.agent_card, 1, 6, 1, 6)
        for widget in self.metrics:
            self.metric_grid.removeWidget(widget)
        for i, widget in enumerate(self.metrics):
            widget.icon.setVisible(not two_metrics)
            self.metric_grid.addWidget(widget, i // 2 if two_metrics else 0, i % 2 if two_metrics else i)
        for i in range(4):
            self.metric_grid.setColumnStretch(i, 1 if i < (2 if two_metrics else 4) else 0)

    def add_current_entry(self):
        self.add_entry("今日任务")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "columns_layout"):
            self.arrange_columns()

    def add_entry(self, section, day=None, parent=None, on_saved=None):
        kind = "schedule" if section == "日程" else "task"
        default_day = None if section == "Todo" else day or self.day
        dialog = UnifiedTaskDialog(self.window, kind=kind, due_date=default_day)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.values()
            def append(context):
                context.checkpoint()
                return self.window.store.add_planned_task(values)
            def saved(_):
                self.refresh()
                self.window.planning_page.refresh()
                self.window.project_page.refresh()
                if on_saved:
                    on_saved(values["due_date"])
            self.window.submit(None, "保存统一任务", append, saved, persist_result=False)

    def open_task_item(self, item):
        row = item.data(ROW_DATA) or {}
        if row.get("planned_task_id"):
            task = self.window.store.planned_task(row["planned_task_id"])
            if task:
                self.edit_unified_task(task)
        else:
            self.open_daily()

    def edit_unified_task(self, task):
        try:
            due = date.fromisoformat(task["due_date"]) if task.get("due_date") else None
        except ValueError:
            due = None
        dialog = UnifiedTaskDialog(self.window, kind=task["kind"], due_date=due,
                                   project_id=task.get("project_id") or "", title=task["title"], record=task)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        def save(context):
            context.checkpoint()
            return self.window.store.update_planned_task(task["id"], values)
        def saved(_):
            self.refresh()
            self.window.planning_page.refresh()
            self.window.project_page.refresh()
        self.window.submit(None, "更新统一任务", save, saved, persist_result=False)

    def set_day(self, day):
        self.follow_today = day == date.today()
        if day != self.day:
            self.day = day
            self.report = None
            self.tasks.setEnabled(False)
            self.open_daily_button.setEnabled(False)
            self.tasks.blockSignals(True)
            self.tasks.clear()
            self.tasks.blockSignals(False)
            self.tasks.addItem("正在读取所选日期…")
        self.date_picker.blockSignals(True)
        self.date_picker.setDate(QDate(day.year, day.month, day.day))
        self.date_picker.blockSignals(False)
        self.refresh()

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
        self.heading.setText("工作台")
        self.date_label.setText(self.day.strftime("%Y 年 %m 月 %d 日") + " · 星期" + "一二三四五六日"[self.day.weekday()])
        is_today = self.day == date.today()
        self.today_button.setEnabled(not is_today)
        self.task_card.title.setText("今日任务" if is_today else "当日任务")
        self.metrics[0].title.setText("今日任务" if is_today else "当日任务")
        notes_root = work_root(self.window.store)
        self.notes_root = notes_root
        daily, root = Daily(notes_root, self.day), self.window.store.setting("project_workspace", "")
        def read(context):
            report = daily.load()
            from ..task_migration import legacy_ref
            migration = self.window.store.setting("legacy_task_import_v1", {})
            imported_files = set(migration.get("daily_files", [])) if migration.get("daily") == str(notes_root.resolve()) else set()
            migrated_refs = self.window.store.planned_task_source_refs()
            occurrences = {}
            if self.day.isoformat() + ".md" in imported_files:
                report["tasks"] = []
            else:
                remaining = []
                for row in report["tasks"]:
                    key = ("task", row["text"])
                    occurrence = occurrences.get(key, 0)
                    occurrences[key] = occurrence + 1
                    source_ref = legacy_ref(f"daily:{notes_root.resolve()}:{self.day.isoformat()}", "task", row["text"], occurrence)
                    if source_ref not in migrated_refs:
                        remaining.append(row)
                report["tasks"] = remaining
            priority_labels = {"high": "高", "medium": "中", "low": "低"}
            for task in self.window.store.planned_tasks(due_date=self.day.isoformat()):
                if task["kind"] != "task":
                    continue
                entry = {"line": -1, "text": task["title"], "title": task["title"],
                         "done": task["status"] == "done", "priority": priority_labels.get(task["priority"], ""),
                         "time": "", "day": task["due_date"], "planned_task_id": task["id"],
                         "project_id": task["project_id"], "source": task["source"], "notes": task["notes"]}
                report["tasks"].append(entry)
            context.checkpoint()
            projects, errors = [], []
            watches = [str(notes_root), str(Path(report["path"]).parent), report["path"]]
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
            plan_report = PlanBook(notes_root).scan(context=context)
            legacy_cycles = {row["text"][2:] for row in report["tasks"]
                             if row.get("line", -1) >= 0 and row["text"].startswith("↻ ")}
            report["tasks"] = [row for row in report["tasks"]
                               if not (row.get("line", -1) >= 0 and row["text"].startswith("↻ "))]
            matched_legacy_cycles, reminders = set(), []
            for item in plan_report["items"]:
                metadata = item["metadata"]
                status = metadata.get("status", "planned")
                if item["type"] == "recurring" and occurs_on(item, self.day):
                    if item["title"] in legacy_cycles:
                        matched_legacy_cycles.add(item["title"])
                    completed_dates = metadata.get("completed_dates", [])
                    is_completed = isinstance(completed_dates, list) and self.day.isoformat() in completed_dates
                    if not is_completed:
                        plan_tasks_meta = metadata.get("frequency", "daily")
                        frequency = {"daily": "每日", "weekly": "每周", "monthly": "每月"}.get(plan_tasks_meta, "周期")
                        reminders.append({"kind": "recurring", "title": item["title"], "day": self.day.isoformat(),
                                          "detail": frequency, "plan_id": item["id"]})
                if item["type"] in {"plan", "goal"} and status not in {"done", "archived"}:
                    due_text = metadata.get("due", "")
                    try:
                        due = date.fromisoformat(due_text)
                    except (TypeError, ValueError):
                        continue
                    if due <= self.day:
                        reminders.append({"kind": "plan_due", "title": item["title"], "day": due_text,
                                          "detail": "今天到期" if due == self.day else "已逾期", "plan_id": item["id"]})
            for title in legacy_cycles - matched_legacy_cycles:
                reminders.append({"kind": "recurring", "title": title, "day": self.day.isoformat(),
                                  "detail": "周期"})
            reminders.sort(key=lambda item: (item["day"], item["kind"], item["title"].casefold()))
            plan_errors = plan_report["errors"]
            watches.append(str(notes_root / "计划"))
            return {"daily": report, "projects": projects, "errors": errors, "watches": watches,
                    "reminders": reminders, "plan_errors": plan_errors}
        self.read_background(read, self.render)

    def render(self, report):
        if not self.matches_day(report["daily"]):
            return
        self.projects = report["projects"]
        self.render_daily(report["daily"], report.get("reminders", []), report.get("plan_errors", []))
        self.render_projects(report)

    def render_daily(self, report, reminders=None, plan_errors=None):
        if not self.matches_day(report):
            return
        self.report = report
        self.refresh_attention(reminders or [])
        self.tasks.blockSignals(True)
        self.tasks.clear()
        for row in self.report["tasks"]:
            title = row.get("title", row["text"])
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole,
                         ("planned", row["planned_task_id"]) if row.get("planned_task_id") else ("markdown", row["line"]))
            if row.get("source") == "ai" and row.get("planned_task_id"):
                item.setToolTip(title + "\nAI 规划任务请在项目概览中交给 Agent 执行。")
            else:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if row["done"] else Qt.CheckState.Unchecked)
            item.setData(ROW_DATA, row)
            if row.get("source") != "ai":
                item.setToolTip(title)
            self.tasks.addItem(item)
        self.tasks.blockSignals(False)
        if not self.tasks.count():
            item = QListWidgetItem("这一天还没有安排。点“添加任务”开始。")
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.tasks.addItem(item)
        self.tasks.fit(4)
        count = len(report["tasks"])
        done = sum(row["done"] for row in report["tasks"])
        self.task_card.title.setText(("今日任务" if self.day == date.today() else "当日任务") + f"  ({done}/{count})")
        hint = "周期任务显示在今日提醒中"
        if plan_errors:
            hint += f" · {len(plan_errors)} 条计划未能读取"
        self.daily_hint.setText(hint)
        self.metrics[0].value.setText(f"{done} / {count}")
        self.metrics[0].progress.setValue(round(done * 100 / count) if count else 0)
        self.metrics[0].note.setText(f"已完成 {round(done * 100 / count)}%" if count else "添加第一项任务")
        self.open_daily_button.setEnabled(True)
        self.refresh_module_tiles()
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
        self.continue_projects.fit(3)
        self.project_card.title.setText(f"继续项目  ({len(active)})")
        self.metrics[1].value.setText(str(len(active)) + " 个")
        self.metrics[1].note.setText("双击项目继续" if active else "在项目页选择工作区")
        self.refresh_module_tiles()
        self.watch(report["watches"])

    def render_agent_activity(self, rows):
        self.agent_rows = rows or []
        running_count = sum(bool(row[3]) for row in self.agent_rows)
        self.agent_items.clear()
        for _, _, resource, running, detail in self.agent_rows[:4]:
            status = "运行中" if running else "空闲"
            item = QListWidgetItem(resource.name + " · " + status)
            item.setData(Qt.ItemDataRole.UserRole, resource.id)
            item.setData(ROW_DATA, {"title": resource.name, "running": running, "detail": detail})
            item.setToolTip(resource.name + "\n" + status + " · " + detail + "\n双击查看 Agent")
            self.agent_items.addItem(item)
        if not self.agent_items.count():
            self.agent_items.addItem("尚未登记工作 Agent。")
        self.agent_items.fit(4)
        total = len(self.agent_rows)
        self.agent_card.title.setText(f"Agent 状态  ({running_count}/{total} 运行中)")
        self.agent_status_hint.setText(
            f"检测到 {running_count} / {total} 个已登记 Agent 正在运行。" if total
            else "在 Agent 页登记常用 Agent 后，这里会显示本机进程状态。")
        self.metrics[2].value.setText(f"{running_count} / {total}")
        self.metrics[2].note.setText("检测到运行进程" if running_count else "暂无 Agent 运行" if total else "先登记常用 Agent")
        self.refresh_module_tiles()

    def refresh_module_tiles(self):
        """Refresh module entry subtitles from already-loaded local page data."""
        if not hasattr(self, "module_buttons"):
            return
        agent_page = getattr(self.window, "agent_page", None)
        environment_page = getattr(self.window, "environment_page", None)
        tracking_page = getattr(self.window, "file_tracking_page", None)
        agents = len(getattr(agent_page, "resources_data", []))
        resources_loaded = bool(getattr(getattr(self.window, "knowledge_page", None), "loaded_once", False))
        environment = getattr(environment_page, "report", None)
        installed = sum(bool(row.get("installed")) for row in environment.get("tools", [])) if environment else None
        roots = getattr(tracking_page, "roots", [])
        events = getattr(tracking_page, "events", [])
        today = date.today().isoformat()
        changes_today = sum(1 for event in events if event.get("time", "").startswith(today))
        details = (
            "今日任务与周月总结",
            f"{sum(row['state'] not in {'completed', 'paused'} for row in self.projects)} 个进行中项目" if self.report else "项目进展与资料",
            f"{len(getattr(self.window.knowledge_page, 'items', []))} 项资料" if resources_loaded else "文件、链接、笔记与标签",
            f"{agents} 个已登记" if agent_page else "登记、Skills 与状态",
            f"{installed} / 8 项工具已安装" if installed is not None else "开发工具与环境变量",
            f"{len(roots)} 个目录 · 今日变更 {changes_today}" if tracking_page else "本机文件变更追踪",
            "外观与工作区偏好",
        )
        for action_button, spec, detail in zip(self.module_buttons, self.module_specs, details):
            action_button.setText(spec[0] + "\n" + detail)

    def open_agent(self, item):
        identity = item.data(Qt.ItemDataRole.UserRole)
        if identity:
            self.window.navigation.setCurrentRow(self.window.AGENT)
            self.window.agent_page.refresh(identity)

    def refresh_attention(self, reminders=None):
        self.attention.clear()
        for reminder in reminders or []:
            if reminder["kind"] == "recurring":
                text = f"{reminder['detail']} · {reminder['title']}"
                data = {**reminder, "target": "plan"}
            else:
                text = f"{reminder['detail']} · {reminder['title']} · {reminder['day']}"
                data = {**reminder, "target": "plan"}
            item = QListWidgetItem(text)
            item.setData(ROW_DATA, data)
            item.setToolTip(text + "\n双击打开对应计划。")
            self.attention.addItem(item)
        count = self.attention.count()
        if not count:
            empty = QListWidgetItem("今天没有周期任务或到期事项提醒。")
            empty.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.attention.addItem(empty)
        self.attention.fit(4)
        self.metrics[3].value.setText(str(count) + " 项")
        self.metrics[3].note.setText("周期任务与已到期事项" if count else "暂无周期或到期提醒")
        self.attention_card.title.setText("今日提醒" + (f"  ({count})" if count else ""))

    def open_plan_records(self):
        self.window.navigation.setCurrentRow(self.window.PLAN)

    def toggle_task(self, item):
        if not self.report or item.data(Qt.ItemDataRole.UserRole) is None:
            return
        target = item.data(Qt.ItemDataRole.UserRole)
        done = item.checkState() == Qt.CheckState.Checked
        if target[0] == "planned":
            identity = target[1]
            status = "done" if done else "planned"
            operation = lambda context: self.window.store.set_planned_task_status(identity, status)
        else:
            daily, expected, line = self.daily(), self.report["original"], target[1]
            operation = lambda context: daily.toggle(line, done, expected)
        def saved(_):
            self.refresh()
            self.window.planning_page.refresh()
            self.window.project_page.refresh()
        self.window.submit(None, "更新今日任务", operation, saved, persist_result=False)
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
        data = item.data(ROW_DATA)
        if not data:
            return
        self.open_plan_records()
        self.window.planning_page.records.search.setText(data["title"])

    def stop_updates(self):
        super().stop_updates()


class CatalogDialog(QDialog):
    def __init__(self, parent, item=None, tool_mode=False, initial_type=None):
        super().__init__(parent)
        self.tool_mode = tool_mode
        self.setWindowTitle(("编辑工具" if item else "添加工具") if tool_mode else ("编辑资源" if item else "收藏新资源"))
        self.resize(700, 540) if tool_mode else self.resize(760, 700)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        panel = QWidget()
        form = QFormLayout(panel)
        form.setContentsMargins(8, 8, 8, 8)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        metadata = item["metadata"] if item else {}
        self.fields = {}
        self.kind = QComboBox(self)
        if tool_mode:
            self.kind.addItem("本地软件", "tool")
            self.kind.addItem("网站", "website")
        else:
            for key, title in TYPES.items():
                self.kind.addItem(title, key)
        self.kind.setCurrentIndex(max(0, self.kind.findData(item["type"] if item else initial_type or ("tool" if tool_mode else "prompt"))))
        form.addRow("类型" if not tool_mode else "添加类型", self.kind)

        if tool_mode:
            self._add_field("name", metadata.get("name", ""), "名称", form, "例如：Visual Studio Code")
            target = QLineEdit(str(metadata.get("launch_target", "") or ""))
            self.fields["launch_target"] = target
            self.target_row = QWidget()
            target_layout = QHBoxLayout(self.target_row)
            target_layout.setContentsMargins(0, 0, 0, 0)
            target_layout.setSpacing(7)
            target_layout.addWidget(target, 1)
            self.browse_button = QPushButton("选择程序…")
            self.browse_button.clicked.connect(self.browse_launch_target)
            target_layout.addWidget(self.browse_button)
            form.addRow("启动程序", self.target_row)
            self._add_field("summary", metadata.get("summary", ""), "一句话用途", form, "例如：代码编辑与调试")
            hint = QLabel("先填名称和启动目标即可；标签、关联和说明可以稍后补充。")
            hint.setObjectName("Subtitle")
            hint.setWordWrap(True)
            form.addRow("", hint)

            self.advanced_toggle = QToolButton()
            self.advanced_toggle.setText("更多设置（可选）")
            self.advanced_toggle.setCheckable(True)
            self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
            form.addRow("", self.advanced_toggle)
            self.advanced_panel = QWidget()
            advanced_form = QFormLayout(self.advanced_panel)
            advanced_form.setContentsMargins(0, 2, 0, 0)
            advanced_form.setVerticalSpacing(9)
            for key, title in (("tags", "标签（逗号分隔）"), ("source", "来源链接"), ("local_path", "本地路径"),
                               ("agents", "适用 Agent（编号 / 类型）"), ("projects", "适用项目（编号）"), ("notes", "我的备注")):
                value = metadata.get(key, "")
                self._add_field(key, value, title, advanced_form)
            self.flags = self._make_flags(metadata)
            advanced_form.addRow("状态", self.flags_widget)
            self.body = QTextEdit()
            self.body.setAcceptRichText(False)
            self.body.setMinimumHeight(140)
            self.body.setPlainText(item["body"] if item else "## 用途\n\n## 使用 / 安装说明\n\n## 配置说明\n\n")
            advanced_form.addRow("Markdown 正文", self.body)
            form.addRow("", self.advanced_panel)
            self.advanced_panel.hide()
            self.advanced_toggle.toggled.connect(self._toggle_advanced)
            self.kind.currentIndexChanged.connect(self._update_tool_type)
            self._update_tool_type()
        else:
            labels = {"name": "名称", "tags": "标签（逗号分隔）", "summary": "一句话用途", "source": "来源链接",
                      "local_path": "本地路径", "agents": "适用 Agent（编号 / 类型，逗号分隔）", "projects": "适用项目（编号，逗号分隔）", "notes": "我的备注"}
            for key, title in labels.items():
                self._add_field(key, metadata.get(key, item["name"] if item and key == "name" else ""), title, form)
            self.flags = self._make_flags(metadata)
            form.addRow("状态", self.flags_widget)
            self.body = QTextEdit()
            self.body.setAcceptRichText(False)
            self.body.setMinimumHeight(210)
            self.body.setPlainText(item["body"] if item else "## 用途\n\n## 使用 / 安装说明\n\n## 配置说明\n\n")
            form.addRow("Markdown 正文", self.body)
        scroll.setWidget(panel)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存工具" if tool_mode and item else "添加工具" if tool_mode else "保存资源")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_field(self, key, value, title, form, placeholder=""):
        field = QLineEdit(", ".join(value) if isinstance(value, list) else str(value or ""))
        field.setPlaceholderText(placeholder)
        if key == "launch_target":
            self.target_label = title
        self.fields[key] = field
        form.addRow(title, field)
        return field

    def _make_flags(self, metadata):
        self.flags = {}
        self.flags_widget = QWidget()
        flags = FlowLayout(self.flags_widget)
        for key, title in (("favorite", "收藏"), ("installed", "已安装"), ("configured", "已配置"), ("tested", "已测试")):
            check = QCheckBox(title)
            check.setChecked(bool(metadata.get(key)))
            self.flags[key] = check
            flags.addWidget(check)
        return self.flags

    def _toggle_advanced(self, expanded):
        self.advanced_panel.setVisible(expanded)
        self.advanced_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def _update_tool_type(self, *_):
        website = self.kind.currentData() == "website"
        self.target_label = "网站地址" if website else "启动程序"
        self.browse_button.setVisible(not website)
        self.fields["launch_target"].setPlaceholderText("https://example.com" if website else "选择程序，或粘贴 .exe / .lnk 路径")
        form = self.target_row.parentWidget().layout()
        for row in range(form.rowCount()):
            field_item = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            if field_item and field_item.widget() is self.target_row:
                label_item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                if label_item and label_item.widget():
                    label_item.widget().setText(self.target_label)
                break

    def browse_launch_target(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择要启动的程序", "", "程序 (*.exe *.com *.lnk);;所有文件 (*)")
        if path:
            self.fields["launch_target"].setText(path)
            if not self.fields["name"].text().strip():
                self.fields["name"].setText(Path(path).stem)

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
    def __init__(self, window, tool_mode=False):
        super().__init__(window)
        self.tool_mode = tool_mode
        self.setObjectName("CatalogPage")
        self.catalog, self.items, self.project_filter, self.agent_filter = Catalog(work_root(window.store)), [], "", ""
        self.loaded_once = False
        self.file_icon_provider = QFileIconProvider()
        self.tool_icon_cache = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 16, 22, 14)
        layout.setSpacing(8)
        heading = QLabel("工具整合" if tool_mode else "资源库")
        heading.setObjectName("Title")
        heading_row = QHBoxLayout()
        heading_icon = QLabel()
        heading_icon.setPixmap(nav_icon(3, "#1a2d50").pixmap(28, 28))
        heading_row.addWidget(heading_icon)
        heading_row.addWidget(heading, 1)
        heading_row.addWidget(control("＋ 添加工具" if tool_mode else "＋ 收藏新资源", lambda: self.edit_item(None), True))
        layout.addLayout(heading_row)
        note = QLabel("登记常用软件和网站，保存本地启动目标与说明；点击即可打开。登记内容使用同一份 Markdown。" if tool_mode else "集中管理知识、文档、代码、模板与工具资源，建立个人知识体系。")
        note.setWordWrap(True)
        note.setObjectName("Subtitle")
        layout.addWidget(note)

        metric_data = (("全部工具", COLORS[0], 0, "软件与网站"), ("本地应用", COLORS[2], 0, "本地启动入口"),
                       ("网站服务", COLORS[3], 0, "网页入口"), ("开发工具", COLORS[0], 0, "按标签统计"),
                       ("常用收藏", COLORS[1], 0, "已收藏工具")) if tool_mode else (
            ("全部资源", COLORS[0], 0, "知识与工作资料"), ("文档资料", COLORS[1], 0, "文章与文档"),
            ("代码仓库", COLORS[0], 0, "GitHub 与代码资料"), ("模板资源", COLORS[3], 0, "模板与 Prompt"),
            ("工具与网站", COLORS[2], 0, "软件与服务"), ("技能与其他", COLORS[3], 0, "Skill、MCP、API 等"))
        self.metrics = [Metric(title, color, icon, hint) for title, color, icon, hint in metric_data]
        metric_row = QHBoxLayout()
        metric_row.setSpacing(8)
        for metric in self.metrics:
            metric_row.addWidget(metric, 1)
        layout.addLayout(metric_row)

        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索资源名称、标签、用途和正文…")
        self.search.setMinimumWidth(200)
        self.search.textChanged.connect(self.render)
        self.kind = QComboBox(self)
        self.kind.addItem("全部类型", "")
        kind_items = (("tool", TYPES["tool"]), ("website", TYPES["website"])) if tool_mode else TYPES.items()
        for key, label in kind_items:
            self.kind.addItem(label, key)
        self.kind.currentIndexChanged.connect(self.render)
        self.kind.hide()
        self.project_choice = QComboBox()
        self.project_choice.addItem("全部项目", "")
        self.project_choice.currentIndexChanged.connect(self.change_project_filter)
        self.tag_choice = QComboBox()
        self.tag_choice.addItem("全部标签", "")
        self.tag_choice.currentIndexChanged.connect(self.render)
        self.favorite = QCheckBox("只看收藏")
        self.favorite.toggled.connect(self.render)
        self.scope = QLabel()
        self.scope.setWordWrap(True)
        self.scope.setObjectName("Subtitle")

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        categories = QWidget()
        categories.setObjectName("CatalogSidePanel")
        side_layout = QVBoxLayout(categories)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(8)
        side_title = QLabel("资源分类")
        side_title.setObjectName("SectionTitle")
        side_layout.addWidget(side_title)
        self.category_list = QListWidget()
        self.category_list.setObjectName("CatalogFilterList")
        self.category_list.currentItemChanged.connect(self.select_category)
        side_layout.addWidget(self.category_list, 2)
        project_title = QLabel("项目筛选")
        project_title.setObjectName("SectionTitle")
        side_layout.addWidget(project_title)
        self.project_list = QListWidget()
        self.project_list.setObjectName("CatalogFilterList")
        self.project_list.currentItemChanged.connect(self.select_project)
        side_layout.addWidget(self.project_list, 1)
        self.hidden_category_panel = categories
        categories.setParent(self)
        categories.hide()

        listing_panel = QFrame()
        listing_panel.setObjectName("CatalogListingPanel")
        list_layout = QVBoxLayout(listing_panel)
        list_layout.setContentsMargins(14, 12, 14, 12)
        list_layout.setSpacing(8)
        list_head = QHBoxLayout()
        self.list_title = QLabel("资源列表（0）")
        self.list_title.setObjectName("SectionTitle")
        list_head.addWidget(self.list_title)
        self.search.setPlaceholderText("搜索资源…")
        self.search.setMaximumWidth(245)
        list_head.addWidget(self.search, 1)
        for widget in (self.project_choice, self.tag_choice, self.favorite):
            row.addWidget(widget)
        row.addWidget(control("刷新", self.refresh))
        self.filter_row = QWidget()
        self.filter_row.setLayout(row)
        self.sort_choice = QComboBox()
        for title, value in (("最近更新", "updated"), ("名称 A–Z", "name"), ("收藏优先", "favorite")):
            self.sort_choice.addItem(title, value)
        self.sort_choice.currentIndexChanged.connect(self.render)
        list_head.addWidget(self.sort_choice)
        self.preview_toggle = QToolButton()
        self.preview_toggle.setText("预览")
        self.preview_toggle.setCheckable(True)
        self.preview_toggle.toggled.connect(self.toggle_preview)
        list_head.addWidget(self.preview_toggle)
        self.list_view_button = QToolButton()
        self.list_view_button.setText("列表")
        self.list_view_button.setCheckable(True)
        self.list_view_button.setChecked(True)
        self.list_view_button.clicked.connect(lambda: self.set_catalog_view(False))
        self.grid_view_button = QToolButton()
        self.grid_view_button.setText("卡片")
        self.grid_view_button.setCheckable(True)
        self.grid_view_button.clicked.connect(lambda: self.set_catalog_view(True))
        list_head.addWidget(self.list_view_button)
        list_head.addWidget(self.grid_view_button)
        list_layout.addLayout(list_head)
        self.type_tabs = QTabBar()
        self.type_tabs.setObjectName("CatalogTypeTabs")
        self.catalog_groups = []
        if not tool_mode:
            for title, kinds in (("全部", ()), ("文档", ("article",)), ("代码", ("github",)),
                                 ("模板", ("template", "prompt")), ("工具", ("tool", "website")),
                                 ("其他", ("skill", "mcp", "api", "agent", "other"))):
                self.type_tabs.addTab(title)
                self.catalog_groups.append(kinds)
            self.type_tabs.currentChanged.connect(self.change_type_group)
        else:
            self.type_tabs.hide()
        list_layout.addWidget(self.type_tabs)
        if not tool_mode:
            list_layout.addWidget(self.filter_row)
            columns = QHBoxLayout()
            columns.setContentsMargins(56, 0, 16, 0)
            for text, stretch in (("名称", 4), ("类型", 1), ("标签", 2), ("更新时间", 1), ("", 0)):
                cell = QLabel(text)
                cell.setObjectName("CatalogColumnHeader")
                columns.addWidget(cell, stretch)
            list_layout.addLayout(columns)
        list_layout.addWidget(self.scope)
        self.listing = QListWidget()
        self.listing.setObjectName("CatalogResourceList")
        self.listing.setWordWrap(True)
        self.listing.setSpacing(4)
        if tool_mode:
            self.listing.setFlow(QListWidget.Flow.LeftToRight)
            self.listing.setWrapping(True)
            self.listing.setResizeMode(QListWidget.ResizeMode.Adjust)
            self.listing.setMovement(QListWidget.Movement.Static)
            self.listing.setIconSize(QSize(46, 46))
        else:
            self.listing.setItemDelegate(CatalogResourceDelegate(self.listing))
            self.listing.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.listing.currentItemChanged.connect(self.selected_changed)
        self.empty_state = QFrame()
        self.empty_state.setObjectName("CatalogEmptyState")
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setContentsMargins(24, 20, 24, 20)
        empty_layout.setSpacing(6)
        self.empty_title = QLabel("资源库还是空的")
        self.empty_title.setObjectName("CatalogEmptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_description = QLabel("上传文件或收藏一条资源，资源副本会保存到资源目录，并以 Markdown 记录。")
        self.empty_description.setObjectName("CatalogEmptyDescription")
        self.empty_description.setWordWrap(True)
        self.empty_description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addStretch(1)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_description)
        empty_layout.addStretch(1)
        list_layout.addWidget(self.empty_state, 1)
        self.catalog_grid = bool(tool_mode)
        self.list_view_button.setChecked(not self.catalog_grid)
        self.grid_view_button.setChecked(self.catalog_grid)
        list_layout.addWidget(self.listing, 1)

        preview = QWidget()
        self.preview_panel = preview
        preview.setObjectName("CatalogDetail")
        pane = QVBoxLayout(preview)
        pane.setContentsMargins(12, 0, 0, 0)
        pane.setSpacing(8)
        detail_header = QHBoxLayout()
        self.detail_icon = QLabel("◇")
        self.detail_icon.setObjectName("CatalogDetailIcon")
        self.detail_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_icon.setFixedSize(54, 54)
        detail_header.addWidget(self.detail_icon)
        title_column = QVBoxLayout()
        self.detail_title = QLabel("选择一个资源")
        self.detail_title.setObjectName("CatalogDetailTitle")
        self.detail_type = QLabel("收藏、整理和查找常用资料")
        self.detail_type.setObjectName("Subtitle")
        title_column.addWidget(self.detail_title)
        title_column.addWidget(self.detail_type)
        detail_header.addLayout(title_column, 1)
        self.detail_favorite = control("☆", self.toggle_favorite)
        self.detail_favorite.setToolTip("收藏 / 取消收藏")
        detail_header.addWidget(self.detail_favorite)
        pane.addLayout(detail_header)
        self.detail_facts = QLabel("")
        self.detail_facts.setObjectName("CatalogDetailFacts")
        self.detail_facts.setWordWrap(True)
        self.detail_facts.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pane.addWidget(self.detail_facts)
        self.detail_summary = QLabel("")
        self.detail_summary.setObjectName("Subtitle")
        self.detail_summary.setWordWrap(True)
        pane.addWidget(self.detail_summary)
        actions = FlowLayout()
        self.edit_button = control("编辑资源", lambda: self.edit_item(self.selected()))
        actions.addWidget(self.edit_button)
        self.favorite_button = control("收藏 / 取消收藏", self.toggle_favorite)
        actions.addWidget(self.favorite_button)
        self.obsidian_button = control("Obsidian 中打开", lambda: self.open_obsidian(self.selected()["path"]) if self.selected() else None)
        actions.addWidget(self.obsidian_button)
        self.source_button = control("打开来源", self.open_source)
        self.local_button = control("查看本地目录", self.open_local)
        self.attachment_button = control("打开附件", self.open_attachment)
        self.attachment_button.setVisible(not tool_mode)
        self.launch_button = control("一键打开", self.launch_tool)
        self.launch_button.setVisible(tool_mode)
        actions.addWidget(self.source_button)
        actions.addWidget(self.local_button)
        actions.addWidget(self.attachment_button)
        actions.addWidget(self.launch_button)
        actions.addWidget(control("显示全部资源", self.clear_scope))
        pane.addLayout(actions)
        self.preview = QTextBrowser()
        self.preview.setObjectName("CatalogBodyPreview")
        self.preview.setOpenExternalLinks(False)
        self.preview.anchorClicked.connect(self.safe_url)
        preview.setMinimumHeight(170)
        preview.setMaximumHeight(280)
        pane.addWidget(self.preview, 1)
        center_split = QSplitter(Qt.Orientation.Vertical)
        self.center_split = center_split
        center_split.addWidget(listing_panel)
        center_split.addWidget(preview)
        center_split.setSizes([900, 0])
        self.preview_enabled = False
        preview.hide()
        split.addWidget(center_split)

        sidebar = QFrame()
        sidebar.setObjectName("CatalogRightColumn")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(10)
        quick_panel = QFrame(); quick_panel.setObjectName("CatalogSidePanel")
        quick_layout = QVBoxLayout(quick_panel)
        quick_layout.setContentsMargins(12, 12, 12, 12)
        quick_layout.setSpacing(8)
        quick_title = QLabel("快捷操作")
        quick_title.setObjectName("SectionTitle")
        quick_layout.addWidget(quick_title)
        quick_grid = QGridLayout()
        if tool_mode:
            self.sidebar_add = control("＋ 添加工具", lambda: self.edit_item(None), True)
            self.sidebar_edit = control("编辑所选", lambda: self.edit_item(self.selected()))
            self.sidebar_open = control("一键打开", self.launch_tool)
            self.sidebar_source = control("打开来源", self.open_source)
            quick_actions = [self.sidebar_add, self.sidebar_edit, self.sidebar_open, self.sidebar_source]
        else:
            quick_actions = [
                control("上传文件", self.upload_file, True),
                control("新建笔记", lambda: self.edit_item(None, initial_type="article")),
                control("新建代码仓库", lambda: self.edit_item(None, initial_type="github")),
                control("新建模板", lambda: self.edit_item(None, initial_type="template")),
                control("添加工具", lambda: self.edit_item(None, initial_type="tool")),
            ]
        quick_icons = (9, 8, 4, 10, 6)
        for index, widget in enumerate(quick_actions):
            widget.setIcon(nav_icon(quick_icons[index % len(quick_icons)], "#2874dc"))
            widget.setMinimumHeight(34)
            quick_grid.addWidget(widget, index // 2, index % 2)
        quick_layout.addLayout(quick_grid)
        sidebar_layout.addWidget(quick_panel)
        tag_panel = QFrame(); tag_panel.setObjectName("CatalogSidePanel")
        tag_layout = QVBoxLayout(tag_panel)
        tag_layout.setContentsMargins(12, 12, 12, 12)
        tag_layout.setSpacing(8)
        tags_title = QLabel("标签云")
        tags_title.setObjectName("SectionTitle")
        tag_layout.addWidget(tags_title)
        self.catalog_tags = QListWidget()
        self.catalog_tags.setObjectName("CatalogTagCloud")
        self.catalog_tags.setFlow(QListWidget.Flow.LeftToRight)
        self.catalog_tags.setWrapping(True)
        self.catalog_tags.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.catalog_tags.setMovement(QListWidget.Movement.Static)
        self.catalog_tags.setMaximumHeight(180)
        self.catalog_tags.itemDoubleClicked.connect(self.choose_catalog_tag)
        tag_layout.addWidget(self.catalog_tags, 1)
        sidebar_layout.addWidget(tag_panel, 1)
        self.catalog_favorites = QListWidget()
        self.catalog_favorites.setObjectName("CatalogFavoriteList")
        self.catalog_favorites.itemDoubleClicked.connect(self.open_catalog_favorite)
        self.catalog_favorites.setMaximumHeight(190)
        split.addWidget(sidebar)
        split.setSizes([900, 320])
        layout.addWidget(split, 4)
        if not tool_mode:
            bottom = QWidget()
            bottom_row = QHBoxLayout(bottom)
            bottom_row.setContentsMargins(0, 0, 0, 0)
            bottom_row.setSpacing(10)
            pending_card = QFrame(); pending_card.setObjectName("TodayCard")
            pending_layout = QVBoxLayout(pending_card)
            pending_layout.setContentsMargins(12, 10, 12, 10)
            self.pending_title = QLabel("待整理资源（0）"); self.pending_title.setObjectName("TodaySectionTitle")
            pending_layout.addWidget(self.pending_title)
            self.pending_list = QListWidget(); self.pending_list.setObjectName("CatalogSummaryList")
            pending_layout.addWidget(self.pending_list)
            favorite_card = QFrame(); favorite_card.setObjectName("TodayCard")
            favorite_layout = QVBoxLayout(favorite_card)
            favorite_layout.setContentsMargins(12, 10, 12, 10)
            self.favorite_title = QLabel("最近收藏（0）"); self.favorite_title.setObjectName("TodaySectionTitle")
            favorite_layout.addWidget(self.favorite_title)
            favorite_layout.addWidget(self.catalog_favorites, 1)
            stats_card = QFrame(); stats_card.setObjectName("TodayCard")
            stats_layout = QVBoxLayout(stats_card)
            stats_layout.setContentsMargins(12, 10, 12, 10)
            stats_title = QLabel("资源统计"); stats_title.setObjectName("TodaySectionTitle")
            stats_layout.addWidget(stats_title)
            self.stats_rows = {}
            for key, title in (("article", "文档"), ("github", "代码"), ("template", "模板"), ("tool", "工具"), ("other", "其他")):
                line = QHBoxLayout()
                text = QLabel(title)
                bar = QProgressBar(); bar.setRange(0, 100); bar.setTextVisible(False); bar.setFixedHeight(8)
                count = QLabel("0")
                line.addWidget(text); line.addWidget(bar, 1); line.addWidget(count)
                stats_layout.addLayout(line)
                self.stats_rows[key] = (bar, count)
            storage_card = QFrame(); storage_card.setObjectName("TodayCard")
            storage_layout = QVBoxLayout(storage_card)
            storage_layout.setContentsMargins(12, 10, 12, 10)
            storage_head = QHBoxLayout()
            storage_title = QLabel("存储空间"); storage_title.setObjectName("TodaySectionTitle")
            self.storage_total = QLabel("0 B")
            storage_head.addWidget(storage_title, 1); storage_head.addWidget(self.storage_total)
            storage_layout.addLayout(storage_head)
            self.storage_details = QListWidget(); self.storage_details.setObjectName("CatalogSummaryList")
            storage_layout.addWidget(self.storage_details, 1)
            for widget in (pending_card, favorite_card, stats_card, storage_card):
                bottom_row.addWidget(widget, 1)
            layout.addWidget(bottom, 1)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setObjectName("Subtitle")
        self.message.hide()

    def refresh(self):
        if self.closed:
            return
        root = work_root(self.window.store)
        if self.catalog.files.root != root.resolve():
            self.catalog = Catalog(root)
        catalog = self.catalog
        self.read_background(catalog.scan, self.loaded)

    def loaded(self, report):
        self.loaded_once = True
        self.items = report["items"]
        self.rebuild_filters()
        self.message.setText("；".join(report["errors"]) if report["errors"] else f"{len(self.items)} 个资源 · 正文保存为 Markdown，与 Obsidian 使用同一份文件。")
        self.render()
        self.watch([str(self.catalog.files.root), str(self.catalog.files.root / "资源"), *[i["path"] for i in self.items]])
        if hasattr(self.window, "agent_page"):
            self.window.agent_page.catalog_updated(self.items)

    def rebuild_filters(self):
        tags = sorted({str(tag) for item in self.items for tag in item["metadata"].get("tags", [])}, key=str.casefold)
        self.tag_choice.blockSignals(True)
        selected_tag = self.tag_choice.currentData()
        self.tag_choice.clear(); self.tag_choice.addItem("全部标签", "")
        for tag in tags:
            self.tag_choice.addItem(tag, tag)
        self.tag_choice.setCurrentIndex(max(0, self.tag_choice.findData(selected_tag)))
        self.tag_choice.blockSignals(False)

        projects = sorted({str(project) for item in self.items for project in item["metadata"].get("projects", [])}, key=str.casefold)
        names = {r.id: r.name for r in self.window.store.resources() if r.kind in {"project", "vault"}}
        self.project_choice.blockSignals(True)
        selected_project = self.project_filter
        self.project_choice.clear(); self.project_choice.addItem("全部项目", "")
        for project in projects:
            self.project_choice.addItem(names.get(project, project), project)
        self.project_choice.setCurrentIndex(max(0, self.project_choice.findData(selected_project)))
        self.project_choice.blockSignals(False)

        counts = {kind: sum(item["type"] == kind for item in self.items) for kind in TYPES}
        favorite_count = sum(bool(item["metadata"].get("favorite")) for item in self.items
                             if not self.tool_mode or item["type"] in {"tool", "website"})
        if self.tool_mode:
            tool_items = [item for item in self.items if item["type"] in {"tool", "website"}]
            dev_tags = {"开发", "开发工具", "编程", "代码"}
            metric_values = (len(tool_items), counts.get("tool", 0), counts.get("website", 0),
                             sum(bool(dev_tags & {str(tag) for tag in item["metadata"].get("tags", [])}) for item in tool_items),
                             sum(bool(item["metadata"].get("favorite")) for item in tool_items))
        else:
            metric_values = (len(self.items), counts.get("article", 0), counts.get("github", 0),
                             counts.get("template", 0) + counts.get("prompt", 0), counts.get("tool", 0) + counts.get("website", 0),
                             sum(counts.get(kind, 0) for kind in ("skill", "mcp", "api", "agent", "other")))
        for metric, value in zip(self.metrics, metric_values):
            metric.value.setText(str(value))

        self.category_list.blockSignals(True)
        self.category_list.clear()
        visible_types = ("tool", "website") if self.tool_mode else tuple(TYPES)
        rows = [("全部工具" if self.tool_mode else "全部资源", "type", "", len(self.items))]
        rows.extend((TYPES[kind], "type", kind, counts[kind]) for kind in visible_types)
        rows.append(("收藏", "favorite", "favorite", favorite_count))
        selected_category = ("favorite", "favorite") if self.favorite.isChecked() else ("type", self.kind.currentData() or "")
        for title, group, value, count in rows:
            cell = QListWidgetItem(f"{title}     {count}")
            cell.setData(Qt.ItemDataRole.UserRole, (group, value))
            self.category_list.addItem(cell)
            if (group, value) == selected_category:
                self.category_list.setCurrentItem(cell)
        self.category_list.blockSignals(False)

        self.project_list.blockSignals(True)
        self.project_list.clear()
        row = QListWidgetItem("全部项目")
        row.setData(Qt.ItemDataRole.UserRole, "")
        self.project_list.addItem(row)
        if not self.project_filter:
            self.project_list.setCurrentItem(row)
        for project in projects:
            count = sum(project in item["metadata"].get("projects", []) for item in self.items)
            cell = QListWidgetItem(f"{names.get(project, project)}   {count}")
            cell.setData(Qt.ItemDataRole.UserRole, project)
            self.project_list.addItem(cell)
            if project == self.project_filter:
                self.project_list.setCurrentItem(cell)
        self.project_list.blockSignals(False)

        self.catalog_tags.clear()
        tag_counts = {}
        for entry in self.items:
            if self.tool_mode and entry["type"] not in {"tool", "website"}:
                continue
            for tag in entry["metadata"].get("tags", []):
                tag_counts[str(tag)] = tag_counts.get(str(tag), 0) + 1
        for tag, count in sorted(tag_counts.items(), key=lambda pair: (-pair[1], pair[0].casefold()))[:24]:
            item = QListWidgetItem(f"{tag}  {count}")
            item.setData(Qt.ItemDataRole.UserRole, tag)
            self.catalog_tags.addItem(item)
        if not tag_counts:
            empty_tag = QListWidgetItem("暂无标签")
            empty_tag.setFlags(Qt.ItemFlag.NoItemFlags)
            self.catalog_tags.addItem(empty_tag)
        self.catalog_favorites.clear()
        favorites = [entry for entry in self.items if entry["metadata"].get("favorite")
                     and (not self.tool_mode or entry["type"] in {"tool", "website"})]
        favorites.sort(key=lambda entry: entry["name"].casefold())
        favorites.sort(key=lambda entry: str(entry["metadata"].get("updated", "")), reverse=True)
        for entry in favorites[:8]:
            item = QListWidgetItem(entry["name"] + "   ·   " + str(entry["metadata"].get("updated") or "未注明")[:10])
            item.setData(Qt.ItemDataRole.UserRole, entry["path"])
            self.catalog_favorites.addItem(item)
        if not favorites:
            empty_favorite = QListWidgetItem("暂无收藏")
            empty_favorite.setFlags(Qt.ItemFlag.NoItemFlags)
            self.catalog_favorites.addItem(empty_favorite)
        if not self.tool_mode:
            self.favorite_title.setText(f"最近收藏（{len(favorites)}）")
        self.update_resource_summaries(counts)

    def choose_catalog_tag(self, item):
        tag = item.data(Qt.ItemDataRole.UserRole)
        index = self.tag_choice.findData(tag)
        if index >= 0:
            self.tag_choice.setCurrentIndex(index)

    def open_catalog_favorite(self, item):
        self.preferred_path = item.data(Qt.ItemDataRole.UserRole) or ""
        self.render()

    def change_project_filter(self, *_):
        self.project_filter = self.project_choice.currentData() or ""
        self.render()

    def change_type_group(self, index):
        self.render()

    def toggle_preview(self, visible):
        self.preview_enabled = bool(visible)
        item = self.selected()
        self.preview_panel.setVisible(self.preview_enabled and item is not None)
        if self.preview_enabled and item is not None:
            height = max(500, self.center_split.height())
            self.center_split.setSizes([int(height * 0.68), int(height * 0.32)])
        if self.preview_enabled and item is None:
            self.preview.setPlainText("选择资源后查看正文与说明。")

    def set_catalog_view(self, grid):
        self.catalog_grid = bool(grid)
        self.list_view_button.setChecked(not self.catalog_grid)
        self.grid_view_button.setChecked(self.catalog_grid)
        if self.tool_mode or self.catalog_grid:
            self.listing.setFlow(QListWidget.Flow.LeftToRight)
            self.listing.setWrapping(True)
            self.listing.setResizeMode(QListWidget.ResizeMode.Adjust)
            self.listing.setMovement(QListWidget.Movement.Static)
            if not self.tool_mode:
                self.listing.setItemDelegate(None)
        else:
            self.listing.setFlow(QListWidget.Flow.TopToBottom)
            self.listing.setWrapping(False)
            self.listing.setResizeMode(QListWidget.ResizeMode.Fixed)
            self.listing.setMovement(QListWidget.Movement.Static)
            self.listing.setItemDelegate(CatalogResourceDelegate(self.listing))
        self.render()

    @staticmethod
    def format_size(size):
        value = float(size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024

    def update_resource_summaries(self, counts):
        if self.tool_mode:
            return
        groups = {
            "article": counts.get("article", 0),
            "github": counts.get("github", 0),
            "template": counts.get("template", 0) + counts.get("prompt", 0),
            "tool": counts.get("tool", 0) + counts.get("website", 0),
            "other": sum(counts.get(kind, 0) for kind in ("skill", "mcp", "api", "agent", "other")),
        }
        maximum = max(groups.values(), default=0)
        for key, count in groups.items():
            bar, value = self.stats_rows[key]
            bar.setValue(round(count / maximum * 100) if maximum else 0)
            value.setText(str(count))

        self.pending_list.clear()
        for entry in self.items:
            metadata = entry["metadata"]
            issues = []
            if not metadata.get("tags"):
                issues.append("未分类")
            if entry["type"] in {"mcp", "api"} and not metadata.get("configured"):
                issues.append("待配置")
            if not str(metadata.get("summary", "")).strip():
                issues.append("缺少简介")
            if issues:
                updated = str(metadata.get("updated") or "未注明")[:10]
                cell = QListWidgetItem(f'{entry["name"]}   ·   {issues[0]}   ·   {updated}')
                cell.setData(Qt.ItemDataRole.UserRole, entry["path"])
                self.pending_list.addItem(cell)
        if not self.pending_list.count():
            empty = QListWidgetItem("资源信息完整，暂无待整理项")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.pending_list.addItem(empty)
        pending_count = sum(bool(self.pending_list.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.pending_list.count()))
        self.pending_title.setText(f"待整理资源（{pending_count}）")

        sizes = {key: 0 for key in groups}
        for entry in self.items:
            kind = entry["type"]
            key = ("article" if kind == "article" else "github" if kind == "github" else
                   "template" if kind in {"template", "prompt"} else "tool" if kind in {"tool", "website"} else "other")
            try:
                attachment = entry["metadata"].get("attachment_path")
                path = self.catalog.files.path(str(attachment)) if attachment else Path(entry["path"])
                sizes[key] += path.stat().st_size
            except (UserError, OSError):
                pass
        total = sum(sizes.values())
        self.storage_total.setText(self.format_size(total))
        self.storage_details.clear()
        for key, title in (("article", "文档"), ("github", "代码"), ("template", "模板"), ("tool", "工具"), ("other", "其他")):
            self.storage_details.addItem(f"{title}   {self.format_size(sizes[key])}")

    def select_category(self, current, _previous=None):
        if current is None:
            return
        group, value = current.data(Qt.ItemDataRole.UserRole)
        self.kind.blockSignals(True)
        self.favorite.blockSignals(True)
        self.kind.setCurrentIndex(max(0, self.kind.findData(value if group == "type" else "")))
        self.favorite.setChecked(group == "favorite")
        self.kind.blockSignals(False)
        self.favorite.blockSignals(False)
        self.render()

    def select_project(self, current, _previous=None):
        if current is None:
            return
        self.project_filter = current.data(Qt.ItemDataRole.UserRole) or ""
        self.project_choice.blockSignals(True)
        self.project_choice.setCurrentIndex(max(0, self.project_choice.findData(self.project_filter)))
        self.project_choice.blockSignals(False)
        self.render()

    def render(self, *_):
        selected = self.selected()
        identity = selected["id"] if selected else ""
        preferred = getattr(self, "preferred_path", "")
        preferred_item = None
        self.listing.blockSignals(True)
        self.listing.clear()
        query, kind, tag = self.search.text().casefold().strip(), self.kind.currentData(), self.tag_choice.currentData()
        self.agent_match_keys = set()
        if self.agent_filter:
            self.agent_match_keys.add(self.agent_filter.casefold())
            resource = next((r for r in self.window.store.resources() if r.id == self.agent_filter), None)
            if resource:
                engine = str(resource.options.get("engine") or "hermes")
                self.agent_match_keys.update({engine.casefold(), engine.casefold().replace(" ", "-")})
                if engine == "Claude Code":
                    self.agent_match_keys.add("claude")
        selected_group = self.type_tabs.currentIndex() if not self.tool_mode else -1
        group_kinds = self.catalog_groups[selected_group] if 0 <= selected_group < len(self.catalog_groups) else ()
        entries = []
        for entry in self.items:
            metadata = entry["metadata"]
            if self.tool_mode and entry["type"] not in {"tool", "website"}:
                continue
            if group_kinds and entry["type"] not in group_kinds:
                continue
            if kind and entry["type"] != kind or self.favorite.isChecked() and not metadata.get("favorite"):
                continue
            if self.project_filter and self.project_filter not in metadata.get("projects", []) and entry["id"] not in self.project_resource_ids():
                continue
            if self.agent_filter and not self.matches_agent(metadata.get("agents", [])):
                continue
            if tag and tag not in metadata.get("tags", []):
                continue
            if query and query not in (entry["name"] + " " + str(metadata) + " " + entry["body"]).casefold():
                continue
            entries.append(entry)
        sort_mode = self.sort_choice.currentData()
        if sort_mode == "name":
            entries.sort(key=lambda entry: entry["name"].casefold())
        elif sort_mode == "favorite":
            entries.sort(key=lambda entry: (not bool(entry["metadata"].get("favorite")), entry["name"].casefold()))
        else:
            entries.sort(key=lambda entry: str(entry["metadata"].get("updated", "")), reverse=True)
        for entry in entries:
            metadata = entry["metadata"]
            summary = str(metadata.get("summary", "")).replace("\n", " ").strip()
            tags = " · ".join(str(tag) for tag in metadata.get("tags", [])[:3])
            agent = ", ".join(metadata.get("agents", [])[:1])
            configured = "已配置" if metadata.get("configured") else "未配置"
            detail = "  ·  ".join(value for value in (TYPES[entry["type"]], tags, agent, configured) if value)
            text = ("★  " if metadata.get("favorite") else "") + entry["name"] + "\n" + (summary[:76] + ("…" if len(summary) > 76 else "") if summary else detail) + "\n" + detail
            cell = QListWidgetItem(text if self.tool_mode or self.catalog_grid else "")
            cell.setSizeHint(QSize(225, 104) if self.tool_mode else QSize(275, 132) if self.catalog_grid else QSize(560, 55))
            cell.setToolTip(entry["name"] + "\n" + (summary or detail))
            cell.setData(Qt.ItemDataRole.UserRole, entry)
            if self.tool_mode:
                icon = self.tool_icon(entry)
                if not icon.isNull():
                    cell.setIcon(icon)
            self.listing.addItem(cell)
            if entry["id"] == identity:
                self.listing.setCurrentItem(cell)
            if entry["path"] == preferred:
                preferred_item = cell
        self.listing.setVisible(bool(entries))
        self.empty_state.setVisible(not entries)
        if not entries:
            if self.tool_mode:
                self.empty_title.setText("还没有登记工具")
                self.empty_description.setText("添加常用软件或网站，保存启动入口和说明。")
            elif self.items:
                self.empty_title.setText("没有找到匹配资源")
                self.empty_description.setText("调整搜索词或筛选条件，查看资源库中的其他内容。")
            else:
                self.empty_title.setText("资源库还是空的")
                self.empty_description.setText("上传文件或收藏一条资源，资源副本会保存到资源目录，并以 Markdown 记录。")
        if preferred_item is not None:
            self.listing.setCurrentItem(preferred_item)
            self.preferred_path = ""
        if self.listing.currentRow() < 0 and self.listing.count():
            self.listing.setCurrentRow(0)
        self.listing.blockSignals(False)
        self.list_title.setText(f"资源列表（{len(entries)}）")
        self.scope.setText("当前筛选：" + (self.project_filter or self.agent_filter) if self.project_filter or self.agent_filter else "")
        self.sync_filter_selection()
        self.selected_changed()

    def sync_filter_selection(self):
        selected_category = ("favorite", "favorite") if self.favorite.isChecked() else ("type", self.kind.currentData() or "")
        self.category_list.blockSignals(True)
        for index in range(self.category_list.count()):
            item = self.category_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == selected_category:
                self.category_list.setCurrentItem(item)
                break
        self.category_list.blockSignals(False)
        self.project_list.blockSignals(True)
        for index in range(self.project_list.count()):
            item = self.project_list.item(index)
            if (item.data(Qt.ItemDataRole.UserRole) or "") == self.project_filter:
                self.project_list.setCurrentItem(item)
                break
        self.project_list.blockSignals(False)

    def project_resource_ids(self):
        return getattr(self, "related_ids", [])

    def tool_icon(self, entry):
        """Load the native Windows icon for a registered .lnk or executable."""
        if not self.tool_mode or entry.get("type") != "tool":
            return QIcon()
        target = str(entry.get("metadata", {}).get("launch_target") or "").strip()
        if not target:
            return QIcon()
        path = Path(target).expanduser()
        cache_key = str(path)
        if cache_key not in self.tool_icon_cache:
            icon = QIcon()
            try:
                if path.is_file() and path.suffix.casefold() in {".lnk", ".exe", ".com"}:
                    icon = self.file_icon_provider.icon(QFileInfo(str(path)))
            except OSError:
                pass
            self.tool_icon_cache[cache_key] = icon
        return self.tool_icon_cache[cache_key]

    def matches_agent(self, references):
        return bool(getattr(self, "agent_match_keys", set()) & {str(value).casefold() for value in references})

    def selected(self):
        cell = self.listing.currentItem()
        return cell.data(Qt.ItemDataRole.UserRole) if cell else None

    def selected_changed(self, *_):
        item = self.selected()
        if hasattr(self, "preview_panel"):
            self.preview_panel.setVisible(bool(item is not None and self.preview_enabled))
        for widget in (self.edit_button, self.favorite_button, self.obsidian_button, self.local_button, self.detail_favorite):
            widget.setEnabled(item is not None)
        if hasattr(self, "attachment_button"):
            self.attachment_button.setVisible(bool(not self.tool_mode and item and item["metadata"].get("attachment_path")))
            self.attachment_button.setEnabled(bool(item and item["metadata"].get("attachment_path")))
        if hasattr(self, "sidebar_edit"):
            self.sidebar_edit.setEnabled(item is not None)
            self.sidebar_source.setEnabled(bool(item and item["metadata"].get("source")))
            if self.tool_mode:
                target = (item["metadata"].get("launch_target") or
                          (item["metadata"].get("source") if item["type"] == "website" else "")) if item else ""
                self.sidebar_open.setEnabled(bool(target))
            else:
                self.sidebar_open.setEnabled(item is not None)
        self.source_button.setEnabled(bool(item and item["metadata"].get("source")))
        if self.tool_mode:
            target = str(item["metadata"].get("launch_target") or
                         (item["metadata"].get("source", "") if item and item["type"] == "website" else "")).strip() if item else ""
            self.launch_button.setEnabled(bool(target))
        if not item:
            self.detail_title.setText("选择一个资源")
            self.detail_type.setText("收藏、整理和查找常用资料")
            self.detail_icon.setText("◇")
            self.detail_icon.setPixmap(QPixmap())
            self.detail_facts.clear()
            self.detail_summary.clear()
            self.preview.setPlainText("还没有匹配资源。点击“收藏新资源”，或将带 frontmatter 的 Markdown 放入工作台的“资源”文件夹。")
            return
        metadata = item["metadata"]
        config_state = "已配置" if metadata.get("configured") else "未配置"
        test_state = "已测试" if metadata.get("tested") else "未测试"
        self.detail_title.setText(item["name"])
        self.detail_type.setText(TYPES.get(item["type"], item["type"]) + "  ·  " + "、".join(metadata.get("tags", [])))
        icons = {"prompt": "▤", "skill": "⬡", "mcp": "↔", "api": "⇄", "agent": "♙", "tool": "⚒", "website": "◎", "github": "⌘", "article": "▧", "template": "▣"}
        app_icon = self.tool_icon(item) if self.tool_mode else QIcon()
        if not app_icon.isNull():
            self.detail_icon.setText("")
            self.detail_icon.setPixmap(app_icon.pixmap(46, 46))
        else:
            self.detail_icon.setPixmap(QPixmap())
            self.detail_icon.setText(icons.get(item["type"], "◇"))
        self.detail_favorite.setText("★" if metadata.get("favorite") else "☆")
        self.favorite_button.setText("取消收藏" if metadata.get("favorite") else "收藏资源")
        resources = self.window.store.resources()
        names = {r.id: r.name for r in resources}
        names.update({str(r.options.get("engine") or "").casefold(): r.name for r in resources if r.kind in {"agent", "hermes_local", "hermes_server"}})
        projects = [names.get(value, value) for value in metadata.get("projects", [])]
        agents = [names.get(value, value) for value in metadata.get("agents", [])]
        file_size = ""
        try:
            attachment = metadata.get("attachment_path")
            size_path = self.catalog.files.path(str(attachment)) if attachment else Path(item["path"])
            file_size = f"{size_path.stat().st_size / 1024:.1f} KB"
        except (UserError, OSError):
            pass
        facts = [f"状态：{config_state} · {test_state}",
                 "所属项目：" + ("、".join(projects) if projects else "默认项目"),
                 "关联 Agent：" + ("、".join(agents) if agents else "未指定"),
                 "来源链接：" + str(metadata.get("source") or "未填写"),
                 "本地目录：" + str(metadata.get("local_path") or Path(item["path"]).parent),
                 "最后更新：" + str(metadata.get("updated") or "未注明")]
        if metadata.get("attachment_path"):
            facts.insert(4, "关联文件：" + str(metadata["attachment_path"]))
        if self.tool_mode:
            facts.insert(4, "启动目标：" + str(metadata.get("launch_target") or "未设置"))
        if file_size:
            facts.append("文件大小：" + file_size)
        self.detail_facts.setText("\n".join(facts))
        self.detail_summary.setText(str(metadata.get("summary", "")) or "未填写简介。")
        text = "## 资源正文\n\n" + item["body"]
        if metadata.get("notes"):
            text += "\n\n## 我的备注\n\n" + str(metadata["notes"])
        self.preview.setMarkdown(text)

    def edit_item(self, item, on_saved=None, initial_type=None):
        dialog = CatalogDialog(self, item, tool_mode=self.tool_mode, initial_type=initial_type)
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
            if not self.tool_mode:
                self.type_tabs.setCurrentIndex(0)
        def saved(report):
            self.preferred_path = report["path"]
            self.refresh()
            if on_saved:
                on_saved()
        self.window.submit(None, "保存资源 Markdown", lambda context: catalog.save(metadata, body, item), saved, persist_result=False)

    def upload_file(self, project_id="", on_saved=None):
        if self.tool_mode:
            return
        source, _ = QFileDialog.getOpenFileName(self, "上传文件到资源库", "", "所有文件 (*)")
        if not source:
            return
        catalog = Catalog(work_root(self.window.store))
        self.window.submit(None, "复制文件并登记到资源库",
                           lambda context: catalog.upload_attachment(source, context, project_id),
                           lambda report: self.upload_finished(report, on_saved), persist_result=False)

    def upload_finished(self, report, on_saved=None):
        self.search.clear()
        self.kind.setCurrentIndex(self.kind.findData("other"))
        self.favorite.setChecked(False)
        self.tag_choice.setCurrentIndex(0)
        self.type_tabs.setCurrentIndex(0)
        self.preferred_path = report["path"]
        self.refresh()
        self.preview_toggle.setChecked(True)
        QMessageBox.information(self, "上传完成", "文件副本已保存，并登记到资源库列表：\n" + report["name"])
        if on_saved:
            on_saved()

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

    def open_attachment(self):
        item = self.selected()
        relative = item["metadata"].get("attachment_path") if item else ""
        if not relative:
            return
        try:
            path = self.catalog.files.path(str(relative))
            if not path.is_file():
                raise UserError("关联的文件副本不存在。")
        except (UserError, OSError) as exc:
            QMessageBox.information(self, "打开附件", str(exc))
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.information(self, "打开附件", "系统未能打开此文件，请检查默认应用设置。")

    def launch_tool(self):
        item = self.selected()
        if not item:
            return
        metadata = item["metadata"]
        target = str(metadata.get("launch_target") or
                     (metadata.get("source", "") if item["type"] == "website" else "")).strip()
        if not target:
            QMessageBox.information(self, "缺少启动目标", "请编辑此条目，填写网址或选择本地程序。")
            return
        url = QUrl(target)
        if url.scheme().lower() in {"https", "http"}:
            self.safe_url(url)
            return
        path = Path(target).expanduser()
        if not path.is_absolute():
            QMessageBox.information(self, "启动目标无效", "本地程序请填写绝对路径。")
            return
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            QMessageBox.information(self, "程序不存在", "请检查本地启动路径，或重新选择程序文件。")
            return
        if not resolved.is_file():
            QMessageBox.information(self, "启动目标无效", "本地启动目标必须是一个文件。")
            return
        if resolved.suffix.casefold() == ".lnk":
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(resolved))):
                QMessageBox.information(self, "启动失败", "系统未能打开此快捷方式，请检查目标程序。")
            return
        if resolved.suffix.casefold() not in {".exe", ".com"}:
            QMessageBox.information(self, "启动目标无效", "本地程序请使用 .exe、.com 或 .lnk 文件，暂不执行脚本。")
            return
        started = QProcess.startDetached(str(resolved), [])
        if isinstance(started, tuple):
            started = started[0]
        if not started:
            QMessageBox.information(self, "启动失败", "系统未能启动该程序，请检查文件类型和权限。")

    def set_scope(self, project="", agent="", related=()):
        self.project_filter, self.agent_filter, self.related_ids = project, agent, list(related)
        self.search.clear()
        self.kind.setCurrentIndex(0)
        if not self.tool_mode:
            self.type_tabs.setCurrentIndex(0)
        self.favorite.setChecked(False)
        self.tag_choice.setCurrentIndex(0)
        self.project_choice.blockSignals(True)
        self.project_choice.setCurrentIndex(max(0, self.project_choice.findData(project)))
        self.project_choice.blockSignals(False)
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
        layout.setContentsMargins(18, 15, 18, 14)
        layout.setSpacing(9)
        heading_row = QHBoxLayout()
        heading = QLabel("Agent 管理")
        heading.setObjectName("Title")
        heading_row.addWidget(heading, 1)
        self.register_agent_button = control("＋ 新建 Agent", lambda: window.add_resource("agent"), True)
        self.register_menu_button = QToolButton()
        self.register_menu_button.setText("⋯")
        self.register_menu_button.setToolTip("登记其他 Agent 类型")
        self.register_menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        register_menu = QMenu(self.register_menu_button)
        register_menu.addAction("登记本地 Hermes", lambda: window.add_resource("hermes_local"))
        register_menu.addAction("登记服务器 Hermes", lambda: window.add_resource("hermes_server"))
        self.register_menu_button.setMenu(register_menu)
        self.refresh_button = control("刷新", self.refresh)
        for widget in (self.refresh_button, self.register_menu_button, self.register_agent_button):
            heading_row.addWidget(widget)
        layout.addLayout(heading_row)
        note = QLabel("管理已登记的 Agent，查看本机操作记录、Skills、配置与备份状态。")
        note.setWordWrap(True)
        note.setObjectName("Subtitle")
        layout.addWidget(note)
        # Keep registered Agents in a compact horizontal card picker.
        list_heading = QHBoxLayout()
        self.list_title = QLabel("已登记的 Agent")
        self.list_title.setObjectName("SectionTitle")
        self.agent_count = QLabel("0 个")
        self.agent_count.setObjectName("Subtitle")
        list_heading.addWidget(self.list_title)
        list_heading.addWidget(self.agent_count)
        list_heading.addStretch(1)
        self.search = QLineEdit()
        self.search.setObjectName("AgentSearch")
        self.search.setPlaceholderText("搜索 Agent…")
        self.search.setMaximumWidth(260)
        self.search.textChanged.connect(self.render_list)
        list_heading.addWidget(self.search)
        layout.addLayout(list_heading)
        self.listing = QListWidget()
        self.listing.setObjectName("AgentListing")
        self.listing.setSpacing(8)
        self.listing.setIconSize(QSize(38, 38))
        self.listing.setFlow(QListWidget.Flow.LeftToRight)
        self.listing.setWrapping(False)
        self.listing.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.listing.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.listing.setMovement(QListWidget.Movement.Static)
        self.listing.setFixedHeight(108)
        self.listing.currentItemChanged.connect(self.render)
        self.listing.itemClicked.connect(self._listing_clicked)
        layout.addWidget(self.listing)
        workspace = QSplitter(Qt.Orientation.Horizontal)
        workspace.setObjectName("AgentWorkspace")
        profile_panel = QWidget()
        profile_panel.setObjectName("AgentProfilePanel")
        profile_panel.setMinimumWidth(420)
        profile_layout = QVBoxLayout(profile_panel)
        profile_layout.setContentsMargins(0, 0, 0, 0)
        profile_layout.setSpacing(9)
        self.hero = QFrame()
        self.hero.setObjectName("AgentHero")
        hero_layout = QVBoxLayout(self.hero)
        hero_layout.setContentsMargins(14, 12, 14, 11)
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

        quick_panel = QFrame()
        quick_panel.setObjectName("AgentQuickPanel")
        quick_layout = QVBoxLayout(quick_panel)
        quick_layout.setContentsMargins(12, 12, 12, 12)
        quick_title = QLabel("快捷操作")
        quick_title.setObjectName("SectionTitle")
        quick_layout.addWidget(quick_title)
        quick_grid = QGridLayout()
        quick_grid.setSpacing(7)
        self.actions = []
        for text, action, primary in (("打开 Agent", self.open_agent, True), ("查看日志", self.records, False),
                             ("编辑配置", self.edit, False), ("打开目录", self.directory, False)):
            widget = control(text, action, primary)
            self.actions.append(widget)
            widget.setMinimumHeight(34)
            position = len(self.actions) - 1
            quick_grid.addWidget(widget, position // 2, position % 2)
        self.lifecycle_buttons = {}
        for index, (name, action) in enumerate((("启动", "start"), ("停止", "stop"), ("重启", "restart"))):
            widget = control(name, lambda checked=False, action=action: self.window.perform(self.selected(), action))
            widget.setMinimumHeight(32)
            widget.setVisible(False)
            widget.setEnabled(False)
            self.lifecycle_buttons[action] = widget
            quick_grid.addWidget(widget, 2 + index // 2, index % 2)
        quick_layout.addLayout(quick_grid)
        quick_layout.addStretch(1)
        self.quick_panel = quick_panel

        self.backup_section = self._section("备份状态", 10)
        self.backup_summary = QLabel("选择 Agent 查看最近备份状态。")
        self.backup_summary.setObjectName("AgentBackupSummary")
        self.backup_summary.setWordWrap(True)
        self.backup_section.body.addWidget(self.backup_summary)
        backup_actions = QHBoxLayout()
        self.backup_button = control("立即备份", self.backup_now, True)
        self.verify_backup_button = control("校验备份", self.verify_backup)
        self.backup_button.setEnabled(False)
        self.verify_backup_button.setEnabled(False)
        backup_actions.addWidget(self.backup_button, 1)
        backup_actions.addWidget(self.verify_backup_button, 1)
        self.backup_section.body.addLayout(backup_actions)

        skills = self._section("Skills", 6, "管理 Skills", self.skills_resources)
        self.skills_preview = QListWidget()
        self.skills_preview.setObjectName("AgentCompactList")
        self.skills_preview.itemDoubleClicked.connect(lambda *_: self.skills_resources())
        skills.body.addWidget(self.skills_preview)

        recent = self._section("最近活动", 13, "查看全部", lambda: self.show_tab(1))
        self.recent_preview = QListWidget()
        self.recent_preview.setObjectName("AgentCompactList")
        self.recent_preview.itemDoubleClicked.connect(
            lambda *_: self.window.show_resource_activity(self.selected()) if self.selected() else None)
        recent.body.addWidget(self.recent_preview)

        sidebar = QWidget()
        sidebar.setObjectName("AgentSidebar")
        sidebar.setMinimumWidth(285)
        sidebar.setMaximumWidth(420)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(9)
        sidebar_layout.addWidget(quick_panel)
        sidebar_layout.addWidget(self.backup_section)
        sidebar_layout.addWidget(skills, 1)
        sidebar_layout.addWidget(recent, 1)

        profile_layout.addWidget(self.hero)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.build_agent_overview(), "概览")
        self.tabs.addTab(self.build_activity_tab(), "操作记录")
        self.tabs.addTab(self.build_skills_tab(), "Skills")
        self.tabs.addTab(self.build_resources_tab(), "相关资源")
        self.tabs.addTab(self.build_notes_tab(), "备注")
        profile_layout.addWidget(self.tabs, 1)
        workspace.addWidget(profile_panel)
        workspace.addWidget(sidebar)
        workspace.setStretchFactor(0, 7)
        workspace.setStretchFactor(1, 3)
        workspace.setSizes([920, 360])
        layout.addWidget(workspace, 1)
        self.resources_data = []
        self.catalog_items = []

    def _section(self, title, icon, action=None, callback=None):
        section = Section(title, icon, action, callback)
        section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return section

    def build_agent_overview(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        usage = self._section("使用统计", 10)
        usage_note = QLabel("仅统计管家本机已记录的操作，不包含 Agent 私有会话、调用次数或运行时长。")
        usage_note.setObjectName("Subtitle")
        usage_note.setWordWrap(True)
        usage.body.addWidget(usage_note)
        self.usage_summary = QLabel("请选择 Agent")
        self.usage_summary.setObjectName("AgentUsageSummary")
        usage.body.addWidget(self.usage_summary)
        self.usage_bars = QWidget()
        self.usage_bars_layout = QHBoxLayout(self.usage_bars)
        self.usage_bars_layout.setContentsMargins(0, 8, 0, 0)
        self.usage_bars_layout.setSpacing(8)
        usage.body.addWidget(self.usage_bars, 1)
        layout.addWidget(usage, 1)
        return page

    def build_activity_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        top = QHBoxLayout()
        top.addWidget(QLabel("此处只显示管家在本机记录的操作，不读取 Agent 私有会话。"), 1)
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
        row.addWidget(control("查看关联知识库资料", self.resources))
        layout.addLayout(row)
        self.resource_list = QListWidget()
        self.resource_list.setObjectName("AgentCompactList")
        self.resource_list.itemDoubleClicked.connect(lambda *_: self.resources())
        layout.addWidget(self.resource_list, 1)
        return page

    def build_skills_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        row = QHBoxLayout()
        row.addWidget(QLabel("查看知识库中关联到当前 Agent 的资料。"), 1)
        row.addWidget(control("在知识库中查看", self.skills_resources))
        layout.addLayout(row)
        self.skills_list = QListWidget()
        self.skills_list.setObjectName("AgentCompactList")
        self.skills_list.itemDoubleClicked.connect(lambda *_: self.resources())
        layout.addWidget(self.skills_list, 1)
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

    def selected(self):
        item = self.listing.currentItem()
        value = item.data(Qt.ItemDataRole.UserRole) if item else None
        return value if value is not None and not isinstance(value, str) else None

    def refresh(self, selected_id=None, resources=None):
        previous = self.selected()
        identity = selected_id or (previous.id if previous else "")
        if resources is None:
            resources = self.window.store.resources()
        self.resources_data = [r for r in resources if r.kind in {"agent", "hermes_local", "hermes_server"}]
        self.render_list(identity)
        self.catalog_items = list(getattr(self.window.catalog_page, "items", []))
        catalog_page = self.window.catalog_page
        if not catalog_page.loaded_once and not catalog_page.worker:
            catalog_page.refresh()

    def catalog_updated(self, items):
        self.catalog_items = list(items)
        self.render()

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
            item = QListWidgetItem(resource.name + "\n" + engine + "\n已登记")
            item.setSizeHint(QSize(230, 88))
            icon = QPixmap(38, 38)
            icon.fill(Qt.GlobalColor.transparent)
            painter = QPainter(icon)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            color = QColor("#7c4dff" if "hermes" in resource.kind else "#2675f5")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(0, 0, 38, 38, 9, 9)
            painter.setPen(QColor("#ffffff"))
            painter.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
            painter.drawText(icon.rect(), Qt.AlignmentFlag.AlignCenter, "H" if "hermes" in resource.kind else engine[:1].upper())
            painter.end()
            item.setIcon(QIcon(icon))
            item.setToolTip(resource.name + "\n" + engine)
            item.setData(Qt.ItemDataRole.UserRole, resource)
            self.listing.addItem(item)
            shown += 1
            if resource.id == identity:
                self.listing.setCurrentItem(item)
        if not shown:
            empty_text = "尚未登记 Agent" if not self.resources_data else "没有匹配的 Agent"
            empty = QListWidgetItem(empty_text)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            empty.setSizeHint(QSize(230, 88))
            self.listing.addItem(empty)
        create_item = QListWidgetItem("＋\n新建 Agent")
        create_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        create_item.setSizeHint(QSize(170, 88))
        create_item.setData(Qt.ItemDataRole.UserRole, "__create_agent__")
        self.listing.addItem(create_item)
        if self.listing.currentRow() < 0 and self.listing.count():
            if shown:
                self.listing.setCurrentRow(0)
        self.listing.blockSignals(False)
        self.agent_count.setText(f"{shown} 个")
        self.list_title.setText("已登记的 Agent")
        self.render()

    def _listing_clicked(self, item):
        if item.data(Qt.ItemDataRole.UserRole) == "__create_agent__":
            self.window.add_resource("agent")

    def render(self, *_):
        resource = self.selected()
        for action in self.actions:
            action.setEnabled(resource is not None)
        for action, button in self.lifecycle_buttons.items():
            button.setVisible(False)
            button.setEnabled(False)
        if not resource:
            self.agent_name.setText("选择一个 Agent")
            self.agent_kind.setText("登记后，这里会显示入口、操作记录和 Skills。")
            self.agent_meta.clear()
            self.agent_path.clear()
            self.copy_path_button.setEnabled(False)
            self.notes_detail.setPlainText("选择一个 Agent 查看备注。")
            self.recent_preview.clear(); self.activity_list.clear()
            self.resource_list.clear(); self.skills_list.clear(); self.skills_preview.clear()
            self.usage_summary.setText("请选择 Agent")
            self.backup_summary.setText("选择 Agent 查看最近备份状态。")
            self.backup_button.setEnabled(False)
            self.verify_backup_button.setEnabled(False)
            self.current_path = ""
            self.current_engine = ""
            while self.usage_bars_layout.count():
                item = self.usage_bars_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            return
        used = self.window.store.evidence("agent-used:" + resource.id) or {}
        options = resource.options
        engine = options.get("engine", "服务器 Hermes" if resource.kind == "hermes_server" else "Hermes")
        self.agent_name.setText(resource.name)
        self.agent_icon.setText("H" if "hermes" in resource.kind else engine[:1].upper())
        self.agent_kind.setText("● 已登记  ·  " + engine)
        capabilities = self.window.service.registry.get(resource).capabilities
        for action, button in self.lifecycle_buttons.items():
            supported = action in capabilities
            button.setVisible(supported)
            button.setEnabled(supported and not self.window.is_busy(resource.id))
        busy = self.window.is_busy(resource.id)
        self.backup_button.setEnabled("backup" in capabilities and not busy)
        self.verify_backup_button.setEnabled("verify" in capabilities and not busy)
        try:
            health = backup_health(self.window.store, resource, self.window.service.backup_root())
            created = readable_time(health.get("created_at") or "")
            created_text = f"最近备份：{created}" if created else "尚无本机备份记录"
            evidence = " · 已校验" if health.get("verified") else " · 待校验" if health.get("source") else ""
            self.backup_summary.setText(f"{health.get('state', '未知')}　{created_text}{evidence}")
        except (OSError, UserError, ValueError):
            self.backup_summary.setText("备份状态暂时无法读取，可在数据安全中查看详情。")
        self.agent_meta.setText("最近从管家打开：" + (readable_time(used["at"]) if used else "尚未打开"))
        data_dir = options.get("home") or options.get("config_dir") or options.get("path") or (options.get("record_paths") or [""])[0]
        self.agent_path.setText("数据目录：" + (str(data_dir) if data_dir else "未设置"))
        self.copy_path_button.setEnabled(bool(data_dir))
        self.current_path = str(data_dir)
        self.current_engine = engine
        note = str(options.get("notes", ""))
        agent_note = next((entry for entry in self.catalog_items
                           if entry["type"] == "agent" and resource.id in entry["metadata"].get("agents", [])), None)
        if agent_note:
            note = agent_note["body"] or str(agent_note["metadata"].get("notes", ""))
        if not note:
            note = "暂无备注。可以记录使用习惯、配置说明或换电脑提醒。"
        self.notes_detail.setMarkdown("# " + resource.name + " · 备注\n\n" + note)

        record_paths = options.get("record_paths", [])
        if isinstance(record_paths, str):
            record_paths = [record_paths] if record_paths else []
        existing_records = sum(Path(path).is_dir() for path in record_paths)
        related = self.related_items(resource)
        tasks = self.window.store.task_summaries(limit=100, resource_id=resource.id)
        self.usage_summary.setText(f"最近记录 {len(tasks)} 条　·　可用记录目录 {existing_records}/{len(record_paths)}")
        while self.usage_bars_layout.count():
            item = self.usage_bars_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        today = date.today()
        daily = {today - timedelta(days=offset): 0 for offset in range(6, -1, -1)}
        for task in tasks:
            stamp = str(task.get("started_at", ""))[:10]
            try:
                day = date.fromisoformat(stamp)
            except ValueError:
                continue
            if day in daily:
                daily[day] += 1
        peak = max(daily.values(), default=0)
        for day, count in daily.items():
            column = QWidget()
            column_layout = QVBoxLayout(column)
            column_layout.setContentsMargins(3, 0, 3, 0)
            count_label = QLabel(str(count))
            count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            bar = QProgressBar()
            bar.setOrientation(Qt.Orientation.Vertical)
            bar.setRange(0, max(1, peak))
            bar.setValue(count)
            bar.setTextVisible(False)
            bar.setFixedWidth(24)
            bar.setMinimumHeight(74)
            bar.setMaximumHeight(100)
            day_label = QLabel(day.strftime("%m-%d"))
            day_label.setObjectName("Subtitle")
            day_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            column_layout.addWidget(count_label)
            column_layout.addWidget(bar, 1, Qt.AlignmentFlag.AlignHCenter)
            column_layout.addWidget(day_label)
            self.usage_bars_layout.addWidget(column, 1)
        if not tasks:
            self.usage_summary.setText("暂无已记录的管家操作；不会推测 Agent 的调用量或运行时长。")
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
        self.resource_list.clear(); self.skills_list.clear(); self.skills_preview.clear()
        for entry in related:
            title = entry["name"] + "  ·  " + TYPES.get(entry["type"], entry["type"])
            self.resource_list.addItem(title)
            if entry.get("type") == "skill":
                self.skills_list.addItem(title)
                self.skills_preview.addItem(title)
        if not related:
            self.resource_list.addItem("暂无关联资源。可从资源库关联。")
        if not self.skills_list.count():
            self.skills_list.addItem("暂无关联的 Skill。可从资源库关联已有 Skill 条目。")
            self.skills_preview.addItem("暂无关联的 Skill")

    def backup_now(self):
        resource = self.selected()
        if resource:
            self.window.perform(resource, "backup")

    def verify_backup(self):
        resource = self.selected()
        if resource:
            self.window.perform(resource, "verify")

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
            QMessageBox.information(self, "服务器 Agent", "此登记用于管理远程 Hermes 服务；当前界面支持查看日志、检查和服务控制，尚未提供远程聊天窗口。")
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
            self.window.knowledge_page.set_scope(agent=resource.id)
            self.window.show_resource_hub()

    def skills_resources(self):
        resource = self.selected()
        if resource:
            self.window.knowledge_page.set_scope(agent=resource.id)
            self.window.show_resource_hub()

    def edit(self):
        if self.selected():
            self.window.edit_resource(self.selected())

    def directory(self):
        resource = self.selected()
        if resource:
            if resource.kind == "hermes_server":
                QMessageBox.information(self, "远程目录", "这是服务器上的目录，不能作为本机文件夹打开。请使用“编辑配置”查看服务器路径。")
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
