from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QSplitter, QTableWidgetItem,
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
    """Browse recent Hermes sessions on demand without persisting message text."""
    def __init__(self, parent, resource, report: dict):
        super().__init__(parent)
        self.window, self.resource = parent, resource
        self.generation = 0
        self.setWindowTitle(resource.name + " · 会话记录")
        self.resize(1040, 700)
        self.setMinimumSize(700, 480)
        layout = QVBoxLayout(self)
        note = QLabel(report["note"])
        note.setWordWrap(True)
        note.setObjectName("Subtitle")
        layout.addWidget(note)
        split = QSplitter(Qt.Orientation.Horizontal)
        listing = QWidget()
        listing_layout = QVBoxLayout(listing)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索会话标题或来源")
        self.search.textChanged.connect(self.filter_rows)
        listing_layout.addWidget(self.search)
        self.table = ReadableTable(0, 3)
        self.table.setHorizontalHeaderLabels(["标题", "来源", "更新时间"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.selected)
        self.rows = report["sessions"]
        listing_layout.addWidget(self.table, 1)
        self.filter_rows("")
        split.addWidget(listing)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        self.heading = QLabel("选择一个会话")
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
        folder = QPushButton("打开 Hermes 目录")
        folder.clicked.connect(lambda: parent.open_path(Path(resource.options["home"])))
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

    def filter_rows(self, query):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for item in self.rows:
            values = [item["title"], item["source"], readable_time(item["time"])]
            if query.casefold() not in " ".join(values).casefold():
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                cell.setData(Qt.ItemDataRole.UserRole, item)
                self.table.setItem(row, column, cell)
        self.table.blockSignals(False)
        if hasattr(self, "reader"):
            self.selected()

    def current_item(self):
        cell = self.table.item(self.table.currentRow(), 0)
        return cell.data(Qt.ItemDataRole.UserRole) if cell else None

    def selected(self):
        if not hasattr(self, "reader"):
            return
        self.generation += 1
        item = self.current_item()
        self.reader.stop()
        self.heading.setText(item.get("title", "") if item else "选择一个会话")
        self.preview.setPlainText("读取中…" if item else "点击左侧条目查看内容。")
        if item:
            self.reader.start(100)

    def read_selected(self):
        if not self.isVisible():
            return
        if self.window.is_busy(self.resource.id):
            self.reader.start(150)
            return
        item = self.current_item()
        if item is None:
            return
        generation = self.generation
        def completed(report):
            if self.isVisible() and generation == self.generation:
                self.preview.setPlainText(report["text"])
        self.window.submit(self.resource, "查看 Hermes 会话",
                           lambda context: self.window.service.read_hermes_item(self.resource, "session", item["id"], context), completed, persist_result=False)

    def done(self, result):
        self.reader.stop()
        super().done(result)
