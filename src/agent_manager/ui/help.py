from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QDialog, QHBoxLayout, QListWidget, QPushButton, QTextBrowser, QVBoxLayout


def guide_path() -> Path:
    return Path(__file__).resolve().parents[1] / "assets/docs/USER_GUIDE.md"


class GuideDialog(QDialog):
    """Read the bundled guide offline, with direct access to each module."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Agent 管家 · 使用说明")
        self.resize(1020, 730)
        self.setMinimumSize(780, 540)
        layout = QVBoxLayout(self)
        content = QHBoxLayout()
        self.sections = QListWidget()
        self.sections.setFixedWidth(185)
        self.sections.addItems([
            "先看这里", "工作台", "本地 Hermes", "服务器 Hermes", "本地项目",
            "其他 Agent", "备份与迁移", "活动记录", "设置", "U 盘与换电脑", "常见问题", "技能与工具",
        ])
        content.addWidget(self.sections)
        self.viewer = QTextBrowser()
        self.viewer.setOpenExternalLinks(True)
        try:
            self.viewer.setMarkdown(guide_path().read_text(encoding="utf-8"))
        except OSError:
            self.viewer.setPlainText("使用说明文件缺失，请重新解压完整运行包；资料和配置不受影响。")
        content.addWidget(self.viewer, 1)
        layout.addLayout(content, 1)
        footer = QHBoxLayout()
        footer.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        layout.addLayout(footer)
        self.sections.currentRowChanged.connect(self.show_section)
        self.sections.setCurrentRow(0)

    def show_section(self, index: int) -> None:
        heading = f"{index + 1}. {self.sections.item(index).text()}" if index >= 0 else ""
        if not heading:
            return
        cursor = self.viewer.document().find(heading)
        if not cursor.isNull():
            cursor.clearSelection()
            self.viewer.setTextCursor(cursor)
            self.viewer.ensureCursorVisible()
            scroll = self.viewer.verticalScrollBar()
            scroll.setValue(scroll.value() + self.viewer.cursorRect(cursor).top() - 8)
