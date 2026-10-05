"""A small cross-date view of existing Markdown plans, not a calendar service."""
from datetime import date, timedelta

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QComboBox, QDateEdit,
    QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView)

from ..workbench import Agenda, work_root
from .workbench import MarkdownPage, control
from .today_widgets import label


class AgendaPanel(MarkdownPage):
    def __init__(self, window, today, dialog):
        super().__init__(window)
        self.today, self.dialog, self.index = today, dialog, None
        self.rows = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = label("查看未来安排和过去未完成的任务。双击一项，进入对应日期编辑或勾选。", "TodayMuted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        filters = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems(["未来 30 天", "所有未完成任务", "全部已登记日程", "自选日期范围"])
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
        self.table.itemSelectionChanged.connect(lambda: self.open_button.setEnabled(self.table.currentRow() >= 0))
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
        custom = mode == 3
        for widget in (self.start, self.end, self.until_label):
            widget.setVisible(mode in (0, 3))
        if mode == 0:
            for picker, value in ((self.start, QDate.currentDate()), (self.end, QDate.currentDate().addDays(29))):
                picker.blockSignals(True)
                picker.setDate(value)
                picker.blockSignals(False)
        self.start.setEnabled(custom)
        self.end.setEnabled(custom)
        self.completed.setEnabled(self.mode.currentIndex() != 1)

    def query(self):
        mode = self.mode.currentIndex()
        start = date.today() if mode == 0 else self.start.date().toPython() if mode == 3 else None
        end = date.today() + timedelta(days=29) if mode == 0 else self.end.date().toPython() if mode == 3 else None
        return start, end, mode == 1, self.completed.isChecked() and mode != 1, str(work_root(self.window.store))

    def reload(self):
        if self.worker:
            self.pending = True
            return
        if self.index:
            self.index.cache.clear()
        self.refresh()

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
            self.index = Agenda(root)
        def apply(report):
            if query != self.query():
                return
            self.rows = report["items"]
            self.table.setRowCount(len(self.rows))
            for row, entry in enumerate(self.rows):
                state = "已完成" if entry["done"] else "未完成（过去日期）" if entry["kind"] == "任务" and entry["day"] < date.today().isoformat() else "待完成" if entry["kind"] == "任务" else "已安排"
                text = entry["title"] + (" @" + entry["time"] if entry["time"] else "")
                for column, value in enumerate((entry["day"], entry["kind"], text, entry["priority"] or "—", state)):
                    item = QTableWidgetItem(value)
                    item.setToolTip(value)
                    self.table.setItem(row, column, item)
            self.table.setEnabled(True)
            self.open_button.setEnabled(self.table.currentRow() >= 0)
            days = len({entry["day"] for entry in self.rows})
            self.status.setText((f"{days} 天 · {len(self.rows)} 项" if self.rows else "暂无安排。可以添加未来日程，或切换范围查看已有计划。") + ("\n" + "\n".join(report["errors"][:5]) if report["errors"] else ""))
            self.watch(report["watches"])
        self.read_background(lambda context: self.index.scan(start, end, tasks_only=tasks_only, completed=completed, context=context), apply)

    def read_failed(self):
        self.status.setText("无法读取日程，请检查资料目录与权限。原文件未改变。")

    def open_selected(self):
        row = self.table.currentRow()
        if self.table.isEnabled() and 0 <= row < len(self.rows):
            self.today.set_day(date.fromisoformat(self.rows[row]["day"]))
            self.dialog.accept()

    def add(self, section):
        row = self.table.currentRow()
        day = date.fromisoformat(self.rows[row]["day"]) if self.table.isEnabled() and 0 <= row < len(self.rows) else None
        self.today.add_entry(section, day=day, parent=self)
        self.reload_timer.start()
