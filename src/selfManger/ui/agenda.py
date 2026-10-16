"""Cross-date views of the existing Markdown plans, not a separate calendar store."""
import calendar
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QComboBox, QDateEdit,
    QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView)

from ..planbook import PlanBook, occurs_on
from ..workbench import Agenda, work_root
from .workbench import MarkdownPage, control
from .today_widgets import label


MODE_UPCOMING = 0
MODE_UNFINISHED = 1
MODE_ALL = 2
MODE_CUSTOM = 3
MODE_WEEK = 4
MODE_MONTH = 5


def agenda_period(mode, today, custom_start=None, custom_end=None):
    """Return an inclusive date range for a plan view; None means unbounded."""
    if mode == MODE_UPCOMING:
        return today, today + timedelta(days=29)
    if mode == MODE_WEEK:
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if mode == MODE_MONTH:
        start = today.replace(day=1)
        return start, today.replace(day=calendar.monthrange(today.year, today.month)[1])
    if mode == MODE_CUSTOM:
        return custom_start, custom_end
    return None, None


class AgendaPanel(MarkdownPage):
    def __init__(self, window, today, dialog):
        super().__init__(window)
        self.today, self.dialog, self.index = today, dialog, None
        self.anchor_date = date.today()
        self.rows = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = label("查看未来安排和过去未完成的任务。双击一项，进入对应日期编辑或勾选。", "TodayMuted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        filters = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems(["未来 30 天", "所有未完成任务", "全部已登记日程", "自选日期范围", "本周", "本月"])
        filters.addWidget(self.mode)
        self.start = QDateEdit(QDate.currentDate())
        self.end = QDateEdit(QDate.currentDate().addDays(29))
        for picker in (self.start, self.end):
            picker.setCalendarPopup(True)
            picker.setDisplayFormat("yyyy-MM-dd")
        filters.addWidget(self.start)
        self.until_label = label("至", "TodayMuted")
        filters.addWidget(self.until_label)
        filters.addWidget(self.end)
        filters.addStretch()
        filters.addWidget(control("刷新", self.reload))
        layout.addLayout(filters)
        self.completed = QCheckBox("显示已完成任务")
        layout.addWidget(self.completed)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["日期", "类型", "安排 / 任务", "优先级", "状态"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemDoubleClicked.connect(lambda _: self.open_selected())
        self.table.itemSelectionChanged.connect(self.selection_changed)
        layout.addWidget(self.table, 1)
        self.status = label("正在读取日程…", "TodayMuted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        footer.addWidget(control("添加任务", lambda: self.add("今日任务")))
        footer.addWidget(control("添加日程", lambda: self.add("日程")))
        footer.addStretch()
        self.open_button = control("打开所选日期", self.open_selected, True)
        self.open_button.setEnabled(False)
        footer.addWidget(self.open_button)
        layout.addLayout(footer)
        self.mode.currentIndexChanged.connect(self.refresh)
        self.start.dateChanged.connect(self.refresh)
        self.end.dateChanged.connect(self.refresh)
        self.completed.toggled.connect(self.refresh)
        self.refresh_fields()

    def refresh_fields(self):
        mode = self.mode.currentIndex()
        custom = mode == MODE_CUSTOM
        start, end = agenda_period(mode, self.anchor_date)
        if mode == MODE_CUSTOM:
            start, end = self.start.date().toPython(), self.end.date().toPython()
        for widget in (self.start, self.end, self.until_label):
            widget.setVisible(mode in (MODE_UPCOMING, MODE_CUSTOM, MODE_WEEK, MODE_MONTH))
        if start and end and mode != MODE_CUSTOM:
            for picker, value in ((self.start, QDate(start.year, start.month, start.day)),
                                  (self.end, QDate(end.year, end.month, end.day))):
                picker.blockSignals(True)
                picker.setDate(value)
                picker.blockSignals(False)
        self.start.setEnabled(custom)
        self.end.setEnabled(custom)
        self.completed.setEnabled(self.mode.currentIndex() != 1)

    def query(self):
        mode = self.mode.currentIndex()
        start, end = agenda_period(mode, self.anchor_date, self.start.date().toPython(), self.end.date().toPython())
        return start, end, mode == MODE_UNFINISHED, self.completed.isChecked() and mode != MODE_UNFINISHED, str(work_root(self.window.store))

    def reload(self):
        self.reload_timer.stop()
        if self.index:
            self.index.cache.clear()
        if self.worker:
            self.pending = True
            return
        self.refresh()

    def set_custom_range(self, start, end):
        """Show a calendar-selected date without losing the regular range controls."""
        self.anchor_date = start
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(MODE_CUSTOM)
        self.mode.blockSignals(False)
        for picker, value in ((self.start, start), (self.end, end)):
            picker.blockSignals(True)
            picker.setDate(QDate(value.year, value.month, value.day))
            picker.blockSignals(False)
        self.refresh()

    def selection_changed(self):
        row = self.table.currentRow()
        valid = self.table.isEnabled() and 0 <= row < len(self.rows)
        self.open_button.setEnabled(valid)

    def refresh(self, *_):
        if self.closed:
            return
        self.refresh_fields()
        query = self.query()
        start, end, tasks_only, completed, root = query
        self.table.setEnabled(False)
        self.open_button.setEnabled(False)
        if start and end and start > end:
            self.status.setText("开始日期不能晚于结束日期。")
            return
        self.status.setText("正在读取日程…")
        if self.worker:
            self.pending = True
            return
        if not self.index or str(self.index.files.root) != root:
            self.index = Agenda(root, self.window.store)
        def apply(report):
            if query != self.query():
                return
            self.rows = report["items"]
            self.table.setRowCount(len(self.rows))
            for row, entry in enumerate(self.rows):
                state = ("已完成" if entry["done"] else "待排期" if not entry["day"] else
                         "未完成（过去日期）" if entry["kind"] == "任务" and entry["day"] < date.today().isoformat() else
                         "待完成" if entry["kind"] == "任务" else "已安排")
                text = entry["title"]
                for column, value in enumerate((entry["day"], entry["kind"], text, entry["priority"] or "—", state)):
                    item = QTableWidgetItem(value)
                    item.setToolTip(value)
                    self.table.setItem(row, column, item)
            self.table.setEnabled(True)
            self.selection_changed()
            days = len({entry["day"] for entry in self.rows})
            self.status.setText((f"{days} 天 · {len(self.rows)} 项" if self.rows else "暂无安排。可以添加未来日程，或切换范围查看已有计划。") + ("\n" + "\n".join(report["errors"][:5]) if report["errors"] else ""))
            self.watch(report["watches"] + [str(Path(work_root(self.window.store)) / "计划")])
            callback = getattr(self, "on_rows_changed", None)
            if callback:
                callback(self.rows)
        def load(context):
            report = self.index.scan(start, end, tasks_only=tasks_only, completed=completed, context=context)
            plan_report = PlanBook(root).scan(context=context)
            report["errors"].extend(plan_report["errors"][:5])
            projection_start = start or date.today()
            projection_end = end or projection_start + timedelta(days=29)
            if (projection_end - projection_start).days > 366:
                projection_end = projection_start + timedelta(days=366)
                report["errors"].append("周期任务投影最多显示未来 367 天；请缩小日期范围。")
            existing_occurrences = {(item["day"], item["title"]) for item in report["items"]
                                    if item["kind"] == "任务" and item["title"].startswith("↻ ")}
            projected = 0
            for rule in plan_report["items"]:
                if rule["type"] != "recurring":
                    continue
                for offset in range((projection_end - projection_start).days + 1):
                    context.checkpoint()
                    day = projection_start + timedelta(days=offset)
                    if occurs_on(rule, day):
                        completed_dates = rule["metadata"].get("completed_dates", [])
                        done = isinstance(completed_dates, list) and day.isoformat() in completed_dates
                        if done and not completed:
                            continue
                        marker = "↻ " + rule["title"]
                        if (day.isoformat(), marker) in existing_occurrences:
                            continue
                        if projected >= 2000:
                            report["errors"].append("计划投影达到 2000 项上限，请缩小日期范围。")
                            break
                        report["items"].append({"line": -1, "text": rule["title"], "done": done,
                            "title": rule["title"], "priority": "", "time": "", "day": day.isoformat(),
                            "kind": "周期任务", "path": rule["path"], "plan_id": rule["id"]})
                        projected += 1
                if projected >= 2000:
                    break
            report["items"].sort(key=lambda item: (item["day"], item["kind"], item["title"].casefold()))
            return report
        self.read_background(load, apply)

    def read_failed(self):
        self.status.setText("无法读取日程，请检查资料目录与权限。原文件未改变。")

    def open_selected(self):
        row = self.table.currentRow()
        if self.table.isEnabled() and 0 <= row < len(self.rows):
            entry = self.rows[row]
            if entry.get("planned_task_id") and not entry["day"]:
                self.window.navigation.setCurrentRow(self.window.TODAY)
                task = self.window.store.planned_task(entry["planned_task_id"])
                if task:
                    self.today.edit_unified_task(task)
                return
            self.today.set_day(date.fromisoformat(entry["day"]))
            if self.dialog is not None:
                self.dialog.accept()
            else:
                self.window.navigation.setCurrentRow(self.window.TODAY)

    def add(self, section):
        row = self.table.currentRow()
        day = date.fromisoformat(self.rows[row]["day"]) if self.table.isEnabled() and 0 <= row < len(self.rows) and self.rows[row]["day"] else None
        self.today.add_entry(section, day=day, parent=self, on_saved=lambda _day: self.reload())

    def add_on_day(self, day, section="今日任务"):
        self.today.add_entry(section, day=day, parent=self, on_saved=lambda _day: self.reload())
