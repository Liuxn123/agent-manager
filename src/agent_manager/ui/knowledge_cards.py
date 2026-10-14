"""Compact resource cards: content and purpose take precedence over storage facts."""
from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import QColor, QFont, QPalette, QPen
from PySide6.QtWidgets import QApplication, QStyledItemDelegate, QStyle

from ..knowledge_resources import resource_type


class ResourceCardDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        entry = index.data(Qt.ItemDataRole.UserRole) or {}
        if not entry:
            return
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(5, 5, -5, -5)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        dark = QApplication.instance().property("appearance") == "dark"
        painter.setPen(QPen(QColor("#3185ff" if selected else "#35455c" if dark else "#e2e9f3")))
        painter.setBrush(QColor("#203954" if dark and selected else "#edf5ff" if selected else "#1b2839" if dark else "#ffffff"))
        painter.drawRoundedRect(rect, 9, 9)
        body = rect.adjusted(13, 10, -13, -10)
        foreground = QColor("#e6edf7" if dark else "#172c48")
        secondary = QColor("#a4b4ca" if dark else "#71829b")
        font = QFont(option.font)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(foreground)
        title_rect = QRect(body.x(), body.y(), body.width() - 22, 25)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(entry.get("name", ""), Qt.TextElideMode.ElideRight, title_rect.width()))
        painter.setPen(QColor("#e9a332" if entry.get("favorite") else "#a6b5ca"))
        painter.drawText(QRect(body.right() - 18, body.y(), 20, 25), Qt.AlignmentFlag.AlignCenter, "★" if entry.get("favorite") else "☆")
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(secondary)
        summary = entry.get("summary") or "添加用途或个人笔记，方便下次使用"
        hits = entry.get("matched_members", [])
        if hits:
            member = next((member for member in entry.get("members", []) if member["relative"] == hits[0]), None)
            if member:
                summary = "命中：" + member.get("filename", member["name"])
        painter.drawText(QRect(body.x(), body.y() + 30, body.width(), 22), Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(summary, Qt.TextElideMode.ElideRight, body.width()))
        painter.setPen(QColor("#79b3ff" if dark else "#2475e8"))
        badge = resource_type(entry)
        if entry["kind"] == "bundle":
            badge += f" · {len(entry['members'])} 份内容"
        tags = " · ".join(entry.get("tags", [])[:2])
        footer = badge + ("  ·  " + tags if tags else "")
        painter.drawText(QRect(body.x(), body.bottom() - 23, body.width(), 22), Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(footer, Qt.TextElideMode.ElideRight, body.width()))
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(240, 112)
