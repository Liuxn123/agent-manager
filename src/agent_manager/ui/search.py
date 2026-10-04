"""Small read-only search surface; results never enter operation history."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPushButton, QTextEdit, QVBoxLayout

from ..domain import Resource, UserError
from ..records import list_records, read_record
from ..security import redact


def search_local(resources: list[Resource], query: str, context) -> dict:
    query = query.strip()
    if len(query) < 2:
        raise UserError("请输入至少两个字的搜索词。")
    matches = []
    scanned = budget = 0
    for resource in resources:
        context.checkpoint()
        if resource.kind == "hermes_local":
            home = Path(resource.options.get("home", ""))
            databases = [home / "state.db", home / "sessions.db"]
            databases += list((home / "profiles").glob("*/state.db"))[:20]
            for path in databases:
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                        db.set_progress_handler(lambda: int(context.cancel.is_set()), 1000)
                        columns = {item[1] for item in db.execute("PRAGMA table_info(sessions)")}
                        if not {"id", "title"}.issubset(columns):
                            continue
                        for identity, title in db.execute("SELECT id,title FROM sessions WHERE title LIKE ? LIMIT 25", ("%" + query + "%",)):
                            matches.append({"name": resource.name + " · " + redact(str(title)), "text": "Hermes 会话标题\n" + redact(str(title)) + "\n\n请在 Hermes 中打开对应会话。", "path": ""})
                except sqlite3.Error:
                    continue
            continue
        if resource.kind != "agent":
            continue
        for item in list_records(resource, context)["records"]:
            context.checkpoint()
            size = min(Path(item["path"]).stat().st_size, 2 * 1024**2)
            if scanned >= 300 or budget + size > 32 * 1024**2 or len(matches) >= 100:
                return {"matches": matches, "limited": True, "scanned": scanned}
            scanned += 1
            budget += size
            text = read_record(resource, item["path"], context)["text"]
            index = text.casefold().find(query.casefold())
            if index >= 0 or query.casefold() in item["name"].casefold():
                excerpt = text[max(0, index - 120):max(0, index) + 800]
                matches.append({"name": resource.name + " · " + item["name"], "text": excerpt, "path": item["path"]})
    return {"matches": matches[:100], "limited": False, "scanned": scanned}


class SearchDialog(QDialog):
    def __init__(self, window, selected: Resource | None = None):
        super().__init__(window)
        self.setWindowTitle("搜索本地记录")
        self.resize(880, 600)
        self.rows = []
        self.resources = [r for r in window.store.resources() if r.kind in {"agent", "hermes_local"}]
        layout = QVBoxLayout(self)
        hint = QLabel("在本机记录中搜索；Hermes 搜索会话标题。结果只在窗口显示，不写入管家日志。")
        hint.setWordWrap(True)
        hint.setObjectName("Subtitle")
        layout.addWidget(hint)
        bar = QHBoxLayout()
        self.scope = QComboBox()
        self.scope.addItem("全部 Agent", None)
        for resource in self.resources:
            self.scope.addItem(resource.name, resource.id)
        if selected:
            self.scope.setCurrentIndex(self.scope.findData(selected.id))
        bar.addWidget(self.scope)
        query = QLineEdit()
        query.setPlaceholderText("项目名、任务或关键词")
        bar.addWidget(query, 1)
        search = QPushButton("搜索")
        search.setProperty("primary", True)
        bar.addWidget(search)
        layout.addLayout(bar)
        self.result_list = QListWidget()
        self.result_list.setMaximumHeight(190)
        layout.addWidget(self.result_list)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview, 1)
        self.status = QLabel("可选择一个 Agent 缩小范围。每次最多读取 300 个文件、32 MiB。")
        self.status.setObjectName("Subtitle")
        layout.addWidget(self.status)
        def completed(report):
            if not self.isVisible():
                return
            self.rows = report["matches"]
            self.result_list.clear()
            self.result_list.addItems([item["name"] for item in self.rows])
            self.status.setText(f"找到 {len(self.rows)} 项" + (" · 达到搜索上限，可缩小范围" if report["limited"] else ""))
            search.setEnabled(True)
        def start():
            identity = self.scope.currentData()
            resources = [r for r in self.resources if identity is None or r.id == identity]
            value = query.text().strip()
            if len(value) < 2:
                self.status.setText("请输入至少两个字的搜索词。")
                return
            self.status.setText("正在搜索…")
            # Keep retry available if a background read fails; submissions are isolated.
            window.submit(None, "搜索本地记录", lambda context: search_local(resources, value, context), completed, persist_result=False)
        search.clicked.connect(start)
        query.returnPressed.connect(start)
        self.result_list.currentRowChanged.connect(lambda row: self.preview.setPlainText(self.rows[row]["text"]) if 0 <= row < len(self.rows) else None)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        layout.addWidget(close)
