from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QSplitter, QTableWidgetItem, QTabWidget,
    QTextEdit, QVBoxLayout, QWidget)

from .components import ReadableTable
from .presentation import readable_time


class TextReportDialog(QDialog):
    def __init__(self, parent, title: str, report: dict):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(960, 620)
        layout = QVBoxLayout(self)
        note = QLabel(report.get("note", ""))
        note.setWordWrap(True)
        layout.addWidget(note)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText(report.get("text") or "暂时没有日志记录。")
        layout.addWidget(self.text, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close.rejected.connect(self.reject)
        layout.addWidget(close)


class HermesLibraryDialog(QDialog):
    """Read content on demand; the main worker never persists these reports."""
    def __init__(self, parent, resource, report: dict):
        super().__init__(parent)
        self.window, self.resource = parent, resource
        self.generation = 0
        self.setWindowTitle(resource.name + " · 会话与技能")
        self.resize(1040, 700)
        self.setMinimumSize(700, 480)
        layout = QVBoxLayout(self)
        note = QLabel(report["note"])
        note.setWordWrap(True)
        note.setObjectName("Subtitle")
        layout.addWidget(note)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.tabs = QTabWidget()
        self.tables = {}
        self.rows = {"session": report["sessions"], "skill": report["skills"]}
        for category, title, headers in [("session", "会话", ["标题", "来源", "更新时间"]), ("skill", "技能", ["名称", "技能文件"] )]:
            page = QWidget()
            pane = QVBoxLayout(page)
            search = QLineEdit()
            search.setPlaceholderText("搜索标题或来源" if category == "session" else "搜索技能名称或目录")
            pane.addWidget(search)
            view = ReadableTable(0, len(headers))
            view.setHorizontalHeaderLabels(headers)
            view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            view.verticalHeader().hide()
            view.setShowGrid(False)
            view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            self.tables[category] = view
            view.itemSelectionChanged.connect(self.selected)
            search.textChanged.connect(lambda query, c=category: self.filter_rows(c, query))
            pane.addWidget(view, 1)
            self.tabs.addTab(page, f"{title}（{len(self.rows[category])}）")
            self.filter_rows(category, "")
        split.addWidget(self.tabs)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        self.heading = QLabel("选择一个会话或技能")
        self.heading.setWordWrap(True)
        body_layout.addWidget(self.heading)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlainText("点击左侧条目查看内容。")
        body_layout.addWidget(self.preview, 1)
        split.addWidget(body)
        split.setSizes([450, 550])
        layout.addWidget(split, 1)
        bar = QHBoxLayout()
        folder = QPushButton("打开技能目录")
        folder.clicked.connect(lambda: parent.open_path(Path(resource.options["home"]) / "skills"))
        bar.addWidget(folder)
        bar.addStretch()
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        close.rejected.connect(self.reject)
        bar.addWidget(close)
        layout.addLayout(bar)
        self.reader = QTimer(self)
        self.reader.setSingleShot(True)
        self.reader.timeout.connect(self.read_selected)
        self.tabs.currentChanged.connect(self.selected)

    def filter_rows(self, category, query):
        view = self.tables[category]
        view.blockSignals(True)
        view.setRowCount(0)
        for item in self.rows[category]:
            values = ([item["title"], item["source"], readable_time(item["time"])] if category == "session" else [item["name"], item["relative"]])
            if query.casefold() not in " ".join(values).casefold():
                continue
            row = view.rowCount()
            view.insertRow(row)
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                cell.setData(Qt.ItemDataRole.UserRole, item)
                view.setItem(row, column, cell)
        view.blockSignals(False)
        if hasattr(self, "reader"):
            self.selected()

    def current_item(self):
        category = "session" if self.tabs.currentIndex() == 0 else "skill"
        view = self.tables[category]
        cell = view.item(view.currentRow(), 0)
        return category, cell.data(Qt.ItemDataRole.UserRole) if cell else None

    def selected(self):
        if not hasattr(self, "reader"):
            return
        self.generation += 1
        category, item = self.current_item()
        self.reader.stop()
        self.heading.setText(item.get("title", item.get("name", "")) if item else "选择一个会话或技能")
        self.preview.setPlainText("读取中…" if item else "点击左侧条目查看内容。")
        if item:
            self.reader.start(100)

    def read_selected(self):
        if not self.isVisible():
            return
        if self.window.is_busy(self.resource.id):
            self.reader.start(150)
            return
        category, item = self.current_item()
        if item is None:
            return
        generation = self.generation
        identity = item["id"] if category == "session" else item["relative"]
        def completed(report):
            if self.isVisible() and generation == self.generation:
                self.preview.setPlainText(report["text"])
        self.window.submit(self.resource, "查看 Hermes " + ("会话" if category == "session" else "技能"),
                           lambda context: self.window.service.read_hermes_item(self.resource, category, identity, context), completed, persist_result=False)

    def done(self, result):
        self.reader.stop()
        super().done(result)
