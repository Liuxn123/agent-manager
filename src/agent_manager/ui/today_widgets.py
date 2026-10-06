"""Compact painted rows and cards for Today; no per-row widgets or background polling."""
from __future__ import annotations

import re
from datetime import date

from PySide6.QtCore import Qt, QSize, QRect, QEvent, QDate
from PySide6.QtGui import QColor, QFont, QPen
from PySide6.QtWidgets import (QFrame, QLabel, QVBoxLayout, QHBoxLayout, QListWidget,
    QStyledItemDelegate, QStyle, QDialog, QFormLayout, QLineEdit, QComboBox,
    QDialogButtonBox, QPushButton, QProgressBar, QSizePolicy, QDateEdit)

from .components import nav_icon
from ..workbench import task_details

ROW_DATA = Qt.ItemDataRole.UserRole + 1
COLORS = ("#3979ff", "#ee5264", "#ed9a2c", "#8760ed")


def label(text, name):
    widget = QLabel(text)
    widget.setObjectName(name)
    return widget


def link(text, callback):
    widget = QPushButton(text)
    widget.setObjectName("TodayLink")
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.clicked.connect(callback)
    return widget


class Metric(QFrame):
    def __init__(self, title, color, icon, note):
        super().__init__()
        self.setObjectName("TodayMetric")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 13)
        tile = QLabel()
        tile.setPixmap(nav_icon(icon, color).pixmap(28, 28))
        tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tile.setFixedSize(48, 48)
        pastel = {COLORS[0]: "#e9f3ff", COLORS[1]: "#fff0f3", COLORS[2]: "#fff4e4", COLORS[3]: "#f2ecff"}
        tile.setStyleSheet("background:" + pastel[color] + ";border-radius:12px;")
        self.icon = tile
        layout.addWidget(tile)
        text = QVBoxLayout()
        text.setSpacing(3)
        self.title = label(title, "TodayMetricTitle")
        text.addWidget(self.title)
        self.value = label("0", "TodayMetricValue")
        text.addWidget(self.value)
        self.note = label(note, "TodayMuted")
        self.note.setWordWrap(True)
        text.addWidget(self.note)
        layout.addLayout(text, 1)
        self.progress = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def add_progress(self):
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.layout().itemAt(1).layout().insertWidget(2, self.progress)


class Section(QFrame):
    def __init__(self, title, icon, action=None, callback=None):
        super().__init__()
        self.setObjectName("TodayCard")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 12, 16, 12)
        self.body.setSpacing(8)
        heading = QHBoxLayout()
        image = QLabel()
        image.setPixmap(nav_icon(icon, COLORS[0]).pixmap(18, 18))
        heading.addWidget(image)
        self.title = label(title, "TodaySectionTitle")
        heading.addWidget(self.title, 1)
        if action:
            heading.addWidget(link(action, callback))
        self.body.addLayout(heading)


class TodayList(QListWidget):
    def __init__(self, kind):
        super().__init__()
        self.kind = kind
        self.setObjectName("TodayList")
        self.setItemDelegate(TodayDelegate(self, kind))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(True)
        self.setMinimumWidth(0)

    def fit(self, maximum=5):
        height = self.itemDelegate().row_height
        self.setFixedHeight(max(1, min(maximum, self.count())) * height + 2)


class TodayDelegate(QStyledItemDelegate):
    def __init__(self, parent, kind):
        super().__init__(parent)
        self.kind = kind
        self.row_height = {"project": 78, "schedule": 54, "recent": 40, "alert": 44}.get(kind, 40)

    def sizeHint(self, option, index):
        return QSize(100, self.row_height)

    def text(self, painter, rect, text, color, bold=False, size=13, strike=False):
        font = QFont(painter.font())
        font.setPixelSize(size)
        font.setBold(bold)
        font.setStrikeOut(strike)
        painter.setFont(font)
        painter.setPen(QColor(color))
        text = painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, max(0, rect.width()))
        painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter, text)

    def paint(self, painter, option, index):
        rect = option.rect
        data = index.data(ROW_DATA) or {}
        painter.save()
        painter.setClipRect(rect)
        if option.state & (QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver):
            painter.fillRect(rect, QColor("#eff5ff"))
        painter.setPen(QColor("#edf1f7"))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        if not data:
            self.text(painter, rect.adjusted(8, 0, -8, 0), index.data() or "", "#8390aa", size=12)
        elif self.kind == "project":
            color = COLORS[data.get("position", 0) % 4]
            tile = QRect(rect.x() + 4, rect.y() + 17, 40, 40)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(tile, 10, 10)
            self.text(painter, tile.adjusted(11, 0, 0, 0), data["name"][:1], "#ffffff", True, 19)
            x = rect.x() + 56
            phase = data["phase"]
            width = min(110, max(48, len(phase) * 13 + 20))
            badge = QRect(rect.right() - width - 4, rect.y() + 8, width, 23)
            painter.setBrush(QColor("#eaf2ff"))
            painter.drawRoundedRect(badge, 10, 10)
            self.text(painter, badge.adjusted(9, 0, -6, 0), phase, COLORS[0], size=11)
            self.text(painter, QRect(x, rect.y() + 6, max(10, badge.x() - x - 8), 25), data["name"], "#192c52", True, 14)
            self.text(painter, QRect(x, rect.y() + 31, rect.right() - x - 4, 20), "下一步：" + data["next_step"], "#71809c", size=12)
            self.text(painter, QRect(x, rect.y() + 52, rect.right() - x - 4, 18), data.get("id", "") + " · 双击继续", "#97a2b6", size=11)
        elif self.kind == "schedule":
            match = re.match(r"^((?:[01]\d|2[0-3]):[0-5]\d(?:\s*[-–—~]\s*(?:[01]\d|2[0-3]):[0-5]\d)?)\s+(.+)$", data["text"])
            clock, title = (match[1], match[2]) if match else ("未定时间", data["text"])
            painter.setPen(QPen(QColor("#e2e9f4"), 2))
            painter.drawLine(rect.x() + 9, rect.top(), rect.x() + 9, rect.bottom())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLORS[0]))
            painter.drawEllipse(rect.x() + 5, rect.center().y() - 4, 8, 8)
            self.text(painter, QRect(rect.x() + 24, rect.y() + 6, rect.width() - 32, 19), clock, COLORS[0], size=11)
            self.text(painter, QRect(rect.x() + 24, rect.y() + 26, rect.width() - 32, 22), title, "#263c60", True)
        elif self.kind in {"task", "focus", "todo"}:
            details = task_details(data["text"])
            checked = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value
            x = rect.x() + 6
            if self.kind == "task":
                box = QRect(x, rect.center().y() - 8, 16, 16)
                painter.setPen(QPen(QColor(COLORS[0] if checked else "#b6c3d8"), 1.5))
                painter.setBrush(QColor(COLORS[0]) if checked else QColor("white"))
                painter.drawRoundedRect(box, 3, 3)
                if checked:
                    painter.setPen(QPen(QColor("white"), 2))
                    painter.drawLine(box.x() + 3, box.y() + 8, box.x() + 7, box.y() + 12)
                    painter.drawLine(box.x() + 7, box.y() + 12, box.x() + 13, box.y() + 4)
            elif self.kind == "focus":
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(COLORS[data.get("position", 0) % 4]))
                painter.drawEllipse(x, rect.center().y() - 9, 18, 18)
                self.text(painter, QRect(x + 5, rect.y(), 15, rect.height()), str(data.get("position", 0) + 1), "white", True, 11)
            end = rect.right() - 5
            if self.kind == "todo":
                badge = QRect(end - 78, rect.center().y() - 10, 78, 20)
                overdue = data.get("day", "") < date.today().isoformat()
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#fff0ef" if overdue else "#edf4ff"))
                painter.drawRoundedRect(badge, 9, 9)
                self.text(painter, badge.adjusted(5, 0, -5, 0), data.get("day", "")[5:] + ("逾期" if overdue else "截止"), "#c04f4b" if overdue else COLORS[0], size=10)
                end -= 85
            if details["time"]:
                self.text(painter, QRect(end - 42, rect.y(), 42, rect.height()), details["time"], "#7e8ba3", size=11)
                end -= 53
            if details["priority"]:
                color = {"高": COLORS[1], "中": COLORS[2], "低": COLORS[0]}[details["priority"]]
                badge = QRect(end - 30, rect.center().y() - 10, 30, 20)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor({"高": "#ffecef", "中": "#fff2df", "低": "#eaf3ff"}[details["priority"]]))
                painter.drawRoundedRect(badge, 9, 9)
                self.text(painter, badge.adjusted(9, 0, 0, 0), details["priority"], color, size=11)
                end -= 40
            self.text(painter, QRect(x + 28, rect.y(), max(10, end - x - 28), rect.height()), details["title"], "#94a0b5" if checked else "#263c60", strike=checked)
        else:
            color = COLORS[1] if self.kind == "alert" else COLORS[0]
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(rect.x() + 5, rect.center().y() - 3, 6, 6)
            end = rect.right() - 5
            if data.get("at") and rect.width() > 380:
                self.text(painter, QRect(end - 115, rect.y(), 115, rect.height()), data["at"], "#8997b0", size=11)
                end -= 123
            self.text(painter, QRect(rect.x() + 22, rect.y(), max(10, end - rect.x() - 22), rect.height()), data.get("title", index.data() or ""), "#344768", size=12)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if self.kind == "task" and index.flags() & Qt.ItemFlag.ItemIsUserCheckable:
            mouse = event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton and QRect(option.rect.x() + 4, option.rect.center().y() - 11, 22, 22).contains(event.position().toPoint())
            keyboard = event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Space
            if mouse or keyboard:
                state = Qt.CheckState.Unchecked if index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value else Qt.CheckState.Checked
                return model.setData(index, state, Qt.ItemDataRole.CheckStateRole)
            return False
        return super().editorEvent(event, model, option, index)


class DailyEntryDialog(QDialog):
    def __init__(self, parent, section, day=None):
        super().__init__(parent)
        is_task = section in {"今日任务", "Todo"}
        self.setWindowTitle("添加 Todo" if section == "Todo" else "添加任务" if is_task else "添加日程")
        self.resize(480, 230)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.date = QDateEdit(QDate(day.year, day.month, day.day) if day else QDate.currentDate())
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        form.addRow("截止日期" if section == "Todo" else "哪一天", self.date)
        self.title = QLineEdit()
        self.title.setMaxLength(240)
        self.title.setPlaceholderText("例如：验证 Hermes 恢复")
        form.addRow("做什么", self.title)
        self.priority = QComboBox()
        self.priority.addItems(["普通", "高", "中", "低"])
        if is_task:
            form.addRow("优先级", self.priority)
        self.time = QLineEdit()
        if not is_task:
            self.time.setPlaceholderText("可选，例如 09:00–10:00")
            form.addRow("时间", self.time)
        layout.addLayout(form)
        layout.addWidget(label("保存到所选日期的 Markdown，与 Obsidian 共用。", "TodayMuted"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("添加")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
        self.title.textChanged.connect(lambda text: buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(bool(text.strip())))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.title.setFocus()

    def values(self):
        return self.title.text(), "" if self.priority.currentText() == "普通" else self.priority.currentText(), self.time.text()


class BackupButton(QPushButton):
    def __init__(self, name, status):
        super().__init__()
        self.name, self.status = name, status
        self.setObjectName("TodayBackup")
        self.setToolTip(name + "\n" + status)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setText(name + "\n" + status)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        metrics = self.fontMetrics()
        self.setText("\n".join(metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(10, self.width() - 16)) for text in (self.name, self.status)))
