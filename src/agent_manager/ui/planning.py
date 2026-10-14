"""The standalone planning workspace, backed by user-owned Markdown files."""
from datetime import date
from datetime import timedelta
import re
from pathlib import Path

from PySide6.QtCore import QDate, QPoint, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog,
    QCalendarWidget, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QFrame, QGridLayout, QScrollArea, QSplitter, QTabBar,
    QPushButton, QTableWidget, QTableWidgetItem, QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
    QStackedWidget, QSizePolicy, QListWidget, QListWidgetItem)

from ..domain import UserError
from ..planbook import KINDS, PlanBook
from ..workbench import Daily, work_root
from .components import nav_icon
from .agenda import MODE_CUSTOM, AgendaPanel
from .workbench import MarkdownPage, control
from .today_widgets import label, Section, Metric, link
from .task_dialog import UnifiedTaskDialog

STATUS_LABELS = {"planned": "计划中", "active": "进行中", "done": "已完成", "archived": "已归档"}
FREQUENCY_LABELS = {"daily": "每天", "weekly": "每周", "monthly": "每月"}
WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def markdown_preserving_linebreaks(text):
    """Make authored single newlines visible in QTextBrowser Markdown previews."""
    output = []
    fence_marker = ""
    fence_length = 0
    for raw_line in str(text or "").splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        newline = raw_line[len(line):]
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence_marker:
            if (fence and fence[1][0] == fence_marker and len(fence[1]) >= fence_length
                    and not fence[2].strip()):
                fence_marker = ""
                fence_length = 0
            output.append(raw_line)
            continue
        if fence:
            fence_marker = fence[1][0]
            fence_length = len(fence[1])
            output.append(raw_line)
            continue
        if newline and line.strip():
            output.append(line.rstrip() + "  " + newline)
        else:
            output.append(raw_line)
    return "".join(output)


class PlanTimelineWidget(QWidget):
    """Render dated long-range plans against one shared horizontal time axis."""

    COLORS = {"planned": "#4d83e8", "active": "#21a77a", "done": "#8492a6"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.records = []
        self.axis_start = date.today()
        self.axis_end = self.axis_start + timedelta(days=30)
        self.setObjectName("PlanTimelineCanvas")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(108)

    def set_items(self, items):
        records = []
        for item in items:
            metadata = item.get("metadata", {})
            if item.get("type") in {"template", "review"} or metadata.get("status") == "archived":
                continue
            try:
                start = date.fromisoformat(str(metadata.get("start", ""))) if metadata.get("start") else None
                due = date.fromisoformat(str(metadata.get("due", ""))) if metadata.get("due") else None
            except ValueError:
                continue
            if start is None and due is None:
                continue
            records.append({**item, "timeline_start": start or due, "timeline_end": due or start})
        records.sort(key=lambda item: (item["timeline_start"], item["timeline_end"], item["title"].casefold()))
        self.records = records
        self.setToolTip("\n".join(
            f"{item['title']} · {item['timeline_start'].isoformat()} — {item['timeline_end'].isoformat()}"
            for item in records
        ))
        if records:
            first = min(item["timeline_start"] for item in records)
            last = max(item["timeline_end"] for item in records)
            span = max(1, (last - first).days)
            padding = max(3, round(span * .04))
            self.axis_start = first - timedelta(days=padding)
            self.axis_end = last + timedelta(days=padding)
        else:
            today = date.today()
            self.axis_start = today.replace(day=1)
            self.axis_end = today + timedelta(days=45)
        shown = min(len(records), 8)
        self.setFixedHeight(max(108, 66 + shown * 34 + (20 if len(records) > shown else 0)))
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text_color = self.palette().color(self.foregroundRole())
        dark = self.palette().color(self.backgroundRole()).lightness() < 128
        muted = QColor("#a8b9d3" if dark else "#7184a3")
        axis_color = QColor("#3a4962" if dark else "#d9e2ef")
        label_width = min(235, max(145, int(self.width() * .24)))
        x_left, x_right = label_width + 8, max(label_width + 24, self.width() - 16)
        axis_y = 31
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#202c40" if dark else "#f7faff"))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 10, 10)
        painter.setPen(QPen(axis_color, 1))
        painter.drawLine(x_left, axis_y, x_right, axis_y)

        total_days = max(1, (self.axis_end - self.axis_start).days)
        for tick in range(5):
            x = round(x_left + (x_right - x_left) * tick / 4)
            tick_day = self.axis_start + timedelta(days=round(total_days * tick / 4))
            painter.setPen(QPen(axis_color, 1))
            painter.drawLine(x, axis_y - 4, x, axis_y + 4)
            painter.setPen(muted)
            alignment = Qt.AlignmentFlag.AlignHCenter
            text_x = x - 42
            if tick == 0:
                alignment = Qt.AlignmentFlag.AlignLeft
                text_x = x
            elif tick == 4:
                alignment = Qt.AlignmentFlag.AlignRight
                text_x = x - 84
            fmt = "%Y-%m" if total_days > 180 else "%m-%d"
            painter.drawText(text_x, 2, 84, 20, alignment | Qt.AlignmentFlag.AlignVCenter, tick_day.strftime(fmt))

        today = date.today()
        if self.axis_start <= today <= self.axis_end:
            today_x = round(x_left + (x_right - x_left) * (today - self.axis_start).days / total_days)
            painter.setPen(QPen(QColor("#ff8d52"), 1, Qt.PenStyle.DashLine))
            painter.drawLine(today_x, axis_y + 7, today_x, self.height() - 7)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#fff0e7" if not dark else "#533e34"))
            painter.drawRoundedRect(today_x - 22, axis_y - 22, 44, 17, 8, 8)
            painter.setPen(QColor("#c96736" if not dark else "#ffc19c"))
            painter.drawText(today_x - 22, axis_y - 22, 44, 17,
                             Qt.AlignmentFlag.AlignCenter, "今天")

        visible = self.records[:8]
        if not visible:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#edf4ff" if not dark else "#263a5e"))
            painter.drawRoundedRect(8, 49, max(120, self.width() - 16), 34, 9, 9)
            painter.setPen(muted)
            painter.drawText(17, 52, max(110, self.width() - 34), 30,
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             "暂无带日期的长期计划；设置开始日期或目标日期后会显示在时间轴上。")
            return

        for index, item in enumerate(visible):
            y = 58 + index * 34
            metadata = item["metadata"]
            name = painter.fontMetrics().elidedText(item["title"], Qt.TextElideMode.ElideRight, label_width - 16)
            painter.setPen(text_color)
            painter.drawText(8, y - 11, label_width - 12, 22,
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
            start, end = item["timeline_start"], item["timeline_end"]
            start_x = round(x_left + (x_right - x_left) * (start - self.axis_start).days / total_days)
            end_x = round(x_left + (x_right - x_left) * (end - self.axis_start).days / total_days)
            color = QColor(self.COLORS.get(metadata.get("status", "planned"), self.COLORS["planned"]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            if end_x - start_x < 7:
                painter.drawRoundedRect(start_x - 5, y - 7, 14, 14, 7, 7)
                painter.setPen(muted)
                painter.drawText(start_x + 13, y - 11, 120, 22,
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 start.isoformat()[5:])
            else:
                bar = painter.fontMetrics().elidedText(item["title"], Qt.TextElideMode.ElideRight,
                                                       max(0, end_x - start_x - 18))
                painter.drawRoundedRect(start_x, y - 9, end_x - start_x, 18, 8, 8)
                painter.setPen(QColor("#ffffff"))
                painter.drawText(start_x + 9, y - 9, max(0, end_x - start_x - 18), 18,
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, bar)
                range_text = f"{start:%m-%d} → {end:%m-%d}"
                range_width = x_right - end_x - 8
                if range_width >= 70:
                    painter.setPen(muted)
                    range_text = painter.fontMetrics().elidedText(range_text, Qt.TextElideMode.ElideRight,
                                                                  range_width)
                    painter.drawText(end_x + 8, y - 10, range_width, 20,
                                     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, range_text)
            painter.setBrush(Qt.BrushStyle.NoBrush)

        if len(self.records) > len(visible):
            painter.setPen(muted)
            painter.drawText(label_width + 8, 59 + len(visible) * 34, x_right - label_width - 8, 18,
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             f"另有 {len(self.records) - len(visible)} 项未显示")


class PlanTimelineDialog(QDialog):
    """Show every dated long-range record and edit the selected record in a dialog."""

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.filtered = []
        self.setWindowTitle("全部时间线")
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["开始日期", "目标日期", "类型", "计划事项", "状态"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.update_actions)
        self.table.cellDoubleClicked.connect(lambda *_: self.edit_selected())
        layout.addWidget(self.table, 1)
        self.status = label("", "TodayMuted")
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.edit_button = control("编辑计划", self.edit_selected, True)
        self.edit_button.setEnabled(False)
        actions.addWidget(self.edit_button)
        actions.addStretch(1)
        actions.addWidget(control("关闭", self.accept))
        layout.addLayout(actions)
        self.refresh()

    def refresh(self):
        self.filtered = [item for item in self.page.records.items
                         if item["type"] not in {"template", "review"}
                         and item["metadata"].get("status") != "archived"
                         and (item["metadata"].get("start") or item["metadata"].get("due"))]
        self.filtered.sort(key=lambda item: (
            item["metadata"].get("start") or item["metadata"].get("due") or "9999-99-99",
            item["metadata"].get("due") or "9999-99-99", item["title"].casefold()))
        self.table.setRowCount(len(self.filtered))
        for row, item in enumerate(self.filtered):
            metadata = item["metadata"]
            values = (metadata.get("start") or "—", metadata.get("due") or "—",
                      KINDS[item["type"]], item["title"],
                      STATUS_LABELS.get(metadata.get("status", "planned"), "未知"))
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(value))
                self.table.setItem(row, column, cell)
        self.status.setText(f"共 {len(self.filtered)} 项带日期计划。")
        self.update_actions()

    def selected(self):
        row = self.table.currentRow()
        return self.filtered[row] if 0 <= row < len(self.filtered) else None

    def update_actions(self):
        item = self.selected()
        self.edit_button.setEnabled(item is not None)

    def edit_selected(self):
        item = self.selected()
        if item:
            self.page.edit_plan_record(item["type"], item, self.refresh)


class PlanDateInput(QWidget):
    """A date field that accepts typed ISO dates or a calendar selection."""
    def __init__(self, value="", parent=None):
        super().__init__(parent)
        self.setObjectName("PlanDateInput")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.editor = QLineEdit()
        self.editor.setObjectName("PlanEntryDate")
        self.editor.setPlaceholderText("YYYY-MM-DD")
        self.editor.setClearButtonEnabled(True)
        self.editor.setText(str(value or ""))
        layout.addWidget(self.editor, 1)
        self.calendar_button = QPushButton()
        self.calendar_button.setObjectName("PlanEntryCalendarButton")
        self.calendar_button.setIcon(nav_icon(8, "#71809e"))
        self.calendar_button.setToolTip("从日历选择日期")
        self.calendar_button.setFixedSize(32, 32)
        self.calendar_button.clicked.connect(self.show_calendar)
        layout.addWidget(self.calendar_button)
        self.calendar_popup = None

    def text(self):
        return self.editor.text()

    def show_calendar(self):
        popup = QDialog(self, Qt.WindowType.Popup)
        popup.setObjectName("PlanEntryCalendarPopup")
        popup_layout = QVBoxLayout(popup)
        popup_layout.setContentsMargins(6, 6, 6, 6)
        calendar = QCalendarWidget(popup)
        calendar.setObjectName("PlanEntryCalendar")
        calendar.setGridVisible(True)
        current = QDate.fromString(self.text().strip(), "yyyy-MM-dd")
        calendar.setSelectedDate(current if current.isValid() else QDate.currentDate())
        calendar.clicked.connect(self._date_selected)
        popup_layout.addWidget(calendar)
        popup.adjustSize()
        popup.move(self.calendar_button.mapToGlobal(QPoint(0, self.calendar_button.height())))
        self.calendar_popup = popup
        popup.show()

    def _date_selected(self, value):
        self.editor.setText(value.toString("yyyy-MM-dd"))
        if self.calendar_popup:
            self.calendar_popup.close()


class PlanEntryDialog(QDialog):
    def __init__(self, parent, kind, item=None, summary_period=None):
        super().__init__(parent)
        self.kind, self.item = kind, item
        self.setObjectName("PlanEntryDialog")
        self.setWindowTitle(("编辑" if item else "新增") + KINDS[kind])
        self.resize(860 if kind in {"plan", "recurring"} else 720,
                    760 if kind in {"plan", "recurring"} else 650)
        if kind in {"plan", "recurring"}:
            self._build_plan_form(item)
            return

        layout = QVBoxLayout(self)
        hint_text = "内容保存在“计划”目录的 Markdown 文件中，可继续用 Obsidian 编辑。"
        hint = label(hint_text, "TodayMuted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        metadata = item["metadata"] if item else {}
        self.title = QLineEdit(str(metadata.get("title", "")))
        self.title.setMaxLength(160)
        form.addRow("名称", self.title)
        self.status = QComboBox()
        for key, value in STATUS_LABELS.items():
            self.status.addItem(value, key)
        self.status.setCurrentIndex(max(0, self.status.findData(metadata.get("status", "planned"))))
        if kind != "review":
            form.addRow("状态", self.status)
        self.summary_period = None
        if kind == "review":
            self.summary_period = QComboBox()
            self.summary_period.addItem("周总结", "weekly")
            self.summary_period.addItem("月总结", "monthly")
            current_period = summary_period or metadata.get("summary_period", "weekly")
            self.summary_period.setCurrentIndex(max(0, self.summary_period.findData(current_period)))
            form.addRow("总结周期", self.summary_period)
            if not item and summary_period:
                today = date.today()
                iso = today.isocalendar()
                self.title.setText(f"周总结 · {iso.year}年第{iso.week:02d}周" if summary_period == "weekly"
                                   else f"月总结 · {today:%Y年%m月}")
        self.start = QLineEdit(str(metadata.get("start", "")))
        self.start.setPlaceholderText("YYYY-MM-DD，可留空")
        self.due = QLineEdit(str(metadata.get("due", "")))
        self.due.setPlaceholderText("YYYY-MM-DD；必填" if kind == "plan" else "YYYY-MM-DD，可选")
        if kind != "review":
            form.addRow("计划开始日期" if kind == "plan" else "开始日期", self.start)
            form.addRow("截止日期" if kind == "plan" else "截止日期（可选）" if kind == "recurring" else "目标日期", self.due)
        self.project_id = QLineEdit(str(metadata.get("project_id", "")))
        self.project_id.setPlaceholderText("可填写项目编号，例如 P-2026-010")
        self.parent_id = QLineEdit(str(metadata.get("parent_id", "")))
        self.parent_id.setPlaceholderText("可填写所属目标编号")
        if kind != "review":
            form.addRow("关联项目编号", self.project_id)
            form.addRow("所属目标编号", self.parent_id)
        self.frequency = QComboBox()
        frequencies = [("daily", FREQUENCY_LABELS["daily"]), ("weekly", FREQUENCY_LABELS["weekly"])]
        if item and metadata.get("frequency") == "monthly":
            frequencies.append(("monthly", FREQUENCY_LABELS["monthly"] + "（已有记录）"))
        for key, value in frequencies:
            self.frequency.addItem(value, key)
        self.frequency.setCurrentIndex(max(0, self.frequency.findData(metadata.get("frequency", "daily"))))
        if kind == "recurring":
            form.addRow("重复周期", self.frequency)
        self.body = QTextEdit()
        self.body.setAcceptRichText(False)
        self.body.setMinimumHeight(220)
        self.body.setPlainText(item["body"] if item else self.default_body(kind, summary_period))
        form.addRow("Markdown 正文", self.body)
        layout.addLayout(form, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_plan_form(self, item):
        metadata = item["metadata"] if item else {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        content = QWidget()
        content.setObjectName("PlanEntryContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 24, 28, 18)
        layout.setSpacing(14)

        heading = QHBoxLayout()
        heading.setSpacing(14)
        icon = QLabel()
        icon.setObjectName("PlanEntryIcon")
        icon.setPixmap(nav_icon(8, "#3979ff").pixmap(28, 28))
        icon.setFixedSize(46, 46)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        heading_text = QVBoxLayout()
        heading_text.setSpacing(4)
        heading_label = QLabel(("编辑" if item else "新增") + "长期任务")
        heading_label.setObjectName("PlanEntryHeading")
        heading_text.addWidget(heading_label)
        description = QLabel("设置任务截止日期，可选填写开始日期；记录保存在计划 Markdown 中。")
        description.setObjectName("PlanEntryDescription")
        description.setWordWrap(True)
        heading_text.addWidget(description)
        heading.addLayout(heading_text, 1)
        layout.addLayout(heading)

        self.heading = heading_text.itemAt(0).widget()
        self.description = description
        self.kind_selector = QComboBox()
        self.kind_selector.setObjectName("PlanEntryKind")
        self.kind_selector.addItem("长期任务", "plan")
        self.kind_selector.addItem("周期任务", "recurring")
        self.kind_selector.setCurrentIndex(max(0, self.kind_selector.findData(self.kind)))
        self.kind_selector.setEnabled(item is None)
        layout.addLayout(self._field_row("任务类型", self.kind_selector))
        self.kind_selector.currentIndexChanged.connect(self.update_plan_kind)
        self.summary_period = None

        self.title = QLineEdit(str(metadata.get("title", "")))
        self.title.setObjectName("PlanEntryTitle")
        self.title.setMaxLength(160)
        self.title.setPlaceholderText("请输入名称")
        layout.addLayout(self._field_row("名称", self.title, required=True))

        self.status = QComboBox()
        self.status.setObjectName("PlanEntryStatus")
        for key, value in STATUS_LABELS.items():
            self.status.addItem(value, key)
        self.status.setCurrentIndex(max(0, self.status.findData(metadata.get("status", "planned"))))
        layout.addLayout(self._field_row("状态", self.status))

        date_group = QFrame()
        date_group.setObjectName("PlanEntryDateGroup")
        date_layout = QVBoxLayout(date_group)
        date_layout.setContentsMargins(16, 14, 16, 12)
        date_layout.setSpacing(9)
        date_columns = QHBoxLayout()
        date_columns.setSpacing(16)
        self.start = self._date_editor(metadata.get("start", ""))
        self.due = self._date_editor(metadata.get("due", ""))
        self.start_column = self._date_field_column("计划开始日期", self.start)
        self.start_caption = self.start_column.itemAt(0).widget()
        date_columns.addLayout(self.start_column, 1)
        divider = QFrame()
        divider.setObjectName("PlanEntryDateDivider")
        divider.setFixedWidth(1)
        date_columns.insertWidget(1, divider)
        self.due_column = self._date_field_column("截止日期", self.due)
        self.due_caption = self.due_column.itemAt(0).widget()
        date_columns.addLayout(self.due_column, 1)
        date_layout.addLayout(date_columns)
        self.date_hint = QLabel()
        self.date_hint.setObjectName("PlanEntryDateHint")
        date_layout.addWidget(self.date_hint)

        recurrence_row = QWidget()
        recurrence_layout = QHBoxLayout(recurrence_row)
        recurrence_layout.setContentsMargins(0, 0, 0, 0)
        recurrence_label = QLabel("重复周期")
        recurrence_label.setObjectName("PlanEntryFieldLabel")
        recurrence_layout.addWidget(recurrence_label)
        self.frequency_label = recurrence_label
        self.frequency = QComboBox()
        self.frequency.setObjectName("PlanEntryFrequency")
        self.frequency.addItem("每天", "daily")
        self.frequency.addItem("每周", "weekly")
        if item and metadata.get("frequency") == "monthly":
            self.frequency.addItem("每月（已有记录）", "monthly")
        self.frequency.setCurrentIndex(max(0, self.frequency.findData(metadata.get("frequency", "daily"))))
        recurrence_layout.addWidget(self.frequency, 1)
        date_layout.addWidget(recurrence_row)
        self.weekdays_panel = QWidget()
        self.weekdays_panel.setObjectName("PlanEntryWeekdays")
        weekdays_layout = QHBoxLayout(self.weekdays_panel)
        weekdays_layout.setContentsMargins(0, 0, 0, 0)
        weekdays_layout.setSpacing(6)
        saved_weekdays = metadata.get("weekdays")
        if not saved_weekdays and item and metadata.get("frequency") == "weekly":
            try:
                saved_weekdays = [date.fromisoformat(metadata["start"]).isoweekday()]
            except (KeyError, TypeError, ValueError):
                saved_weekdays = []
        self.weekday_checks = []
        for weekday, weekday_name in enumerate(WEEKDAY_LABELS, start=1):
            checkbox = QCheckBox(weekday_name)
            checkbox.setObjectName("PlanEntryWeekday")
            checkbox.setChecked(weekday in (saved_weekdays or []))
            self.weekday_checks.append(checkbox)
            weekdays_layout.addWidget(checkbox)
        date_layout.addWidget(self.weekdays_panel)
        self.frequency.currentIndexChanged.connect(self.update_plan_kind)
        layout.addWidget(date_group)

        references = QHBoxLayout()
        references.setSpacing(18)
        self.project_id = QLineEdit(str(metadata.get("project_id", "")))
        self.project_id.setObjectName("PlanEntryProjectId")
        self.project_id.setPlaceholderText("可填写项目编号，例如 P-2026-010")
        self.parent_id = QLineEdit(str(metadata.get("parent_id", "")))
        self.parent_id.setObjectName("PlanEntryParentId")
        self.parent_id.setPlaceholderText("可填写所属目标编号")
        references.addLayout(self._field_column("关联项目编号", self.project_id), 1)
        references.addLayout(self._field_column("所属目标编号", self.parent_id), 1)
        layout.addLayout(references)

        body_label = QLabel("Markdown 正文")
        body_label.setObjectName("PlanEntryFieldLabel")
        layout.addWidget(body_label)
        self.body = QTextEdit()
        self.body.setObjectName("PlanEntryBody")
        self.body.setAcceptRichText(False)
        self.body.setMinimumHeight(138)
        self.body.setPlainText(item["body"] if item else self.default_body("plan"))
        layout.addWidget(self.body, 1)
        root.addWidget(content, 1)

        footer = QFrame()
        footer.setObjectName("PlanEntryFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(28, 13, 28, 13)
        footer_layout.addStretch(1)
        cancel = QPushButton("取消")
        save = QPushButton("保存")
        save.setProperty("primary", True)
        save.setDefault(True)
        save.clicked.connect(self.confirm)
        cancel.clicked.connect(self.reject)
        footer_layout.addWidget(cancel)
        footer_layout.addWidget(save)
        root.addWidget(footer)
        self.update_plan_kind()

    def update_plan_kind(self, *_):
        if not hasattr(self, "kind_selector"):
            return
        self.kind = self.kind_selector.currentData()
        recurring = self.kind == "recurring"
        self.setWindowTitle(("编辑" if self.item else "新增") + KINDS[self.kind])
        if hasattr(self, "heading"):
            self.heading.setText(("编辑" if self.item else "新增") + ("周期任务" if recurring else "长期任务"))
            self.description.setText(
                "设置重复开始日期和重复规则；每周可选择多个星期，截止日期可选。"
                if recurring else "设置任务截止日期，可选填写开始日期；记录保存在计划 Markdown 中。")
        self.start_caption.setText("重复开始日期" if recurring else "计划开始日期")
        self.due_caption.setText("截止日期（可选）" if recurring else "截止日期")
        self.date_hint.setText(
            "ⓘ  任务从开始日期起按所选周期重复；每周可勾选一个或多个星期，截止日期可选。"
            if recurring else "ⓘ  截止日期必填；开始日期可选。输入 YYYY-MM-DD 或点日历选择。")
        self.frequency_label.setVisible(recurring)
        self.frequency.setVisible(recurring)
        self.weekdays_panel.setVisible(
            recurring and self.frequency.currentData() == "weekly")

    @staticmethod
    def _field_row(text, field, required=False):
        row = QHBoxLayout()
        field_label = QLabel(text + (" <span style='color:#ef5b5b'>*</span>" if required else ""))
        if required:
            field_label.setTextFormat(Qt.TextFormat.RichText)
        field_label.setObjectName("PlanEntryFieldLabel")
        field_label.setFixedWidth(112)
        row.addWidget(field_label)
        row.addWidget(field, 1)
        return row

    @staticmethod
    def _field_column(text, field):
        column = QVBoxLayout()
        column.setSpacing(6)
        field_label = QLabel(text)
        field_label.setObjectName("PlanEntryFieldLabel")
        column.addWidget(field_label)
        column.addWidget(field)
        return column

    @staticmethod
    def _date_field_column(text, field):
        column = QVBoxLayout()
        column.setSpacing(6)
        field_label = QLabel(text)
        field_label.setObjectName("PlanEntryFieldLabel")
        column.addWidget(field_label)
        control = QFrame()
        control.setObjectName("PlanEntryDateControl")
        control_layout = QHBoxLayout(control)
        control_layout.setContentsMargins(5, 2, 5, 2)
        control_layout.setSpacing(4)
        control_layout.addWidget(field, 1)
        column.addWidget(control)
        return column

    @staticmethod
    def _date_editor(value):
        return PlanDateInput(value)

    @staticmethod
    def default_body(kind, summary_period=None):
        if kind == "template":
            return "## 今日任务\n\n- 示例任务\n"
        if kind == "review":
            if summary_period == "monthly":
                return "## 本月完成\n\n## 主要进展\n\n## 问题与复盘\n\n## 下月计划\n"
            return "## 完成情况\n\n## 未完成与原因\n\n## 下一阶段安排\n"
        return "## 说明\n\n"

    def confirm(self):
        if not self.title.text().strip():
            QMessageBox.information(self, "计划名称", "请填写名称。")
            return
        if self.kind in {"plan", "recurring"}:
            for field_name, editor in (("开始日期", self.start), ("截止日期", self.due)):
                value = self._date_value(editor)
                if value:
                    parsed = QDate.fromString(value, "yyyy-MM-dd")
                    if not parsed.isValid() or parsed.toString("yyyy-MM-dd") != value:
                        QMessageBox.information(self, "计划日期格式", f"{field_name}请使用 YYYY-MM-DD 格式，或从日历中选择。")
                        editor.editor.setFocus()
                        editor.editor.selectAll()
                        return
            if self.kind == "plan" and not self._date_value(self.due):
                QMessageBox.information(self, "截止日期", "请填写长期任务的截止日期。")
                self.due.editor.setFocus()
                return
            if self.kind == "recurring" and not self._date_value(self.start):
                QMessageBox.information(self, "开始日期", "请填写周期任务的重复开始日期。")
                self.start.editor.setFocus()
                return
            if (self.kind == "recurring" and self.frequency.currentData() == "weekly"
                    and not any(box.isChecked() for box in self.weekday_checks)):
                QMessageBox.information(self, "重复星期", "每周任务至少选择一个重复星期。")
                return
        self.accept()

    @staticmethod
    def _date_value(editor):
        return editor.text().strip()

    def values(self):
        previous = self.item["metadata"] if self.item else {}
        return {"title": self.title.text().strip(),
                "status": previous.get("status", "planned") if self.kind == "review" else self.status.currentData(),
                "start": self._date_value(self.start) if self.kind in {"plan", "recurring"} else self.start.text().strip(),
                "due": self._date_value(self.due) if self.kind in {"plan", "recurring"} else self.due.text().strip(),
                "project_id": previous.get("project_id", "") if self.kind == "review" else self.project_id.text().strip(),
                "parent_id": previous.get("parent_id", "") if self.kind == "review" else self.parent_id.text().strip(),
                "frequency": self.frequency.currentData() if self.kind == "recurring" else None,
                "weekdays": ([index for index, box in enumerate(self.weekday_checks, start=1) if box.isChecked()]
                             if self.kind == "recurring" and self.frequency.currentData() == "weekly" else []),
                "progress": previous.get("progress", 0),
                "summary_period": self.summary_period.currentData() if self.summary_period else ""}


class PlanRecordsPanel(MarkdownPage):
    CATEGORIES = [
        ("", "全部"), ("plan", "长期任务"), ("recurring", "周期任务"),
    ]

    def __init__(self, window):
        super().__init__(window)
        self.setObjectName("PlanRecordsPanel")
        self.book = PlanBook(work_root(window.store))
        self.items = []
        self.filtered = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)

        heading = QHBoxLayout()
        heading.setSpacing(12)
        icon = QLabel()
        icon.setObjectName("PlanManagerIcon")
        icon.setPixmap(nav_icon(8, "#2879f6").pixmap(23, 23))
        icon.setFixedSize(48, 48)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(icon)
        heading_text = QVBoxLayout()
        heading_text.setSpacing(2)
        title = QLabel("目标与规划")
        title.setObjectName("PlanManagerTitle")
        description = QLabel("管理长期任务和周期任务，集中查看截止日期与重复安排。")
        description.setObjectName("PlanManagerDescription")
        description.setWordWrap(True)
        heading_text.addWidget(title)
        heading_text.addWidget(description)
        heading.addLayout(heading_text, 1)
        layout.addLayout(heading)

        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName("PlanManagerSearch")
        self.search.setPlaceholderText("搜索计划、项目编号或正文…")
        self.search.textChanged.connect(self.render)
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(control("刷新", self.refresh))
        toolbar.addWidget(control("＋ 新建计划", self.create_item, True))
        layout.addLayout(toolbar)

        split = QSplitter(Qt.Orientation.Horizontal)
        list_panel = QWidget()
        list_panel.setObjectName("PlanManagerListPanel")
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(12, 12, 12, 12)
        list_layout.setSpacing(12)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.kind_filter = QTabBar()
        self.kind_filter.setObjectName("PlanManagerFilter")
        for kind, name in self.CATEGORIES:
            index = self.kind_filter.addTab(name)
            self.kind_filter.setTabData(index, kind)
        self.kind_filter.setExpanding(False)
        self.kind_filter.setUsesScrollButtons(False)
        self.kind_filter.currentChanged.connect(self.render)
        filter_row.addWidget(self.kind_filter)
        self.filtered_count = QLabel()
        self.filtered_count.setObjectName("PlanManagerCount")
        filter_row.addWidget(self.filtered_count, 1)
        list_layout.addLayout(filter_row)

        self.table = QTableWidget(0, 5)
        self.table.setObjectName("PlanManagerTable")
        self.table.setHorizontalHeaderLabels(["类型", "名称", "状态", "开始日期", "目标日期"])
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(52)
        for column, width in ((0, 78), (2, 82), (3, 100), (4, 100)):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(column, width)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.render_detail)
        self.table.cellDoubleClicked.connect(lambda *_: self.edit_item())
        list_layout.addWidget(self.table, 1)
        split.addWidget(list_panel)

        detail = QWidget()
        detail.setObjectName("PlanManagerDetailPanel")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(14, 14, 14, 14)
        detail_layout.setSpacing(12)
        detail_heading = QLabel("计划详情")
        detail_heading.setObjectName("PlanDetailSectionTitle")
        detail_layout.addWidget(detail_heading)

        detail_scroll = QScrollArea()
        detail_scroll.setObjectName("PlanDetailScroll")
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        detail_content = QWidget()
        detail_content.setObjectName("PlanDetailContent")
        detail_content_layout = QVBoxLayout(detail_content)
        detail_content_layout.setContentsMargins(0, 0, 2, 0)
        detail_content_layout.setSpacing(9)

        title_card = QFrame()
        title_card.setObjectName("PlanDetailTitleCard")
        title_layout = QHBoxLayout(title_card)
        title_layout.setContentsMargins(13, 11, 13, 11)
        title_icon = QLabel()
        title_icon.setObjectName("PlanDetailIcon")
        title_icon.setPixmap(nav_icon(8, "#2879f6").pixmap(21, 21))
        title_icon.setFixedSize(46, 46)
        title_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_layout.addWidget(title_icon)
        self.detail_title = QLabel("从左侧选择计划查看详情")
        self.detail_title.setObjectName("PlanDetailTitle")
        self.detail_title.setWordWrap(True)
        title_layout.addWidget(self.detail_title, 1)
        self.edit_button = control("编辑计划", self.edit_item, True)
        self.edit_button.setEnabled(False)
        title_layout.addWidget(self.edit_button)
        self.delete_button = control("删除计划", self.delete_item)
        self.delete_button.setObjectName("PlanDeleteButton")
        self.delete_button.setProperty("danger", True)
        self.delete_button.setEnabled(False)
        title_layout.addWidget(self.delete_button)
        detail_content_layout.addWidget(title_card)
        self.detail_hint = QLabel("选择计划后，点击“编辑计划”修改名称、日期、关联内容和正文。")
        self.detail_hint.setObjectName("PlanManagerDescription")
        self.detail_hint.setWordWrap(True)
        detail_content_layout.addWidget(self.detail_hint)

        self.detail_meta = {}
        metadata_card = QFrame()
        metadata_card.setObjectName("PlanDetailMetadata")
        metadata_grid = QGridLayout(metadata_card)
        metadata_grid.setContentsMargins(12, 10, 12, 10)
        metadata_grid.setHorizontalSpacing(8)
        metadata_grid.setVerticalSpacing(7)
        metadata_grid.setColumnStretch(1, 1)
        metadata_grid.setColumnStretch(3, 1)
        detail_fields = (
            ("类型", "kind"), ("状态", "status"),
            ("开始日期", "start"), ("目标日期", "due"),
            ("关联项目", "project_id"), ("所属目标", "parent_id"),
            ("周期", "frequency"),
        )
        for index, (caption, key) in enumerate(detail_fields):
            row, pair = divmod(index, 2)
            column = pair * 2
            caption_label = QLabel(caption)
            caption_label.setObjectName("PlanDetailMetaLabel")
            value_label = QLabel("—")
            value_label.setObjectName("PlanDetailMetaValue")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            metadata_grid.addWidget(caption_label, row, column)
            metadata_grid.addWidget(value_label, row, column + 1)
            self.detail_meta[key] = value_label
        detail_content_layout.addWidget(metadata_card)

        body_heading = QLabel("正文")
        body_heading.setObjectName("PlanDetailBodyHeading")
        detail_content_layout.addWidget(body_heading)
        self.detail_body = QTextBrowser()
        self.detail_body.setObjectName("PlanDetailBody")
        self.detail_body.setOpenLinks(False)
        self.detail_body.setOpenExternalLinks(False)
        self.detail_body.setMinimumHeight(96)
        self.detail_body.setMaximumHeight(210)
        self.detail_body.setPlaceholderText("暂无正文")
        detail_content_layout.addWidget(self.detail_body, 1)
        detail_scroll.setWidget(detail_content)
        detail_layout.addWidget(detail_scroll, 1)

        actions = QHBoxLayout()
        self.status_button = control("标记完成 / 重新打开", self.toggle_done)
        self.apply_button = control("套用到日期", self.apply_template, True)
        self.target_day = QDateEdit(QDate.currentDate())
        self.target_day.setCalendarPopup(True)
        self.target_day.setDisplayFormat("yyyy-MM-dd")
        actions.addWidget(self.status_button)
        actions.addWidget(self.target_day)
        actions.addWidget(self.apply_button)
        self.target_day.hide()
        self.apply_button.hide()
        detail_layout.addLayout(actions)
        split.addWidget(detail)
        split.setStretchFactor(0, 9)
        split.setStretchFactor(1, 11)
        split.setSizes([640, 780])
        layout.addWidget(split, 1)
        self.status = label("计划正文保存在 Markdown；归档会保留文件。", "TodayMuted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.refresh()

    def select_kind(self, kind):
        if kind == "goal":
            kind = "plan"
        index = next((i for i in range(self.kind_filter.count())
                      if self.kind_filter.tabData(i) == (kind or "")), -1)
        self.kind_filter.setCurrentIndex(max(0, index))

    def selected(self):
        row = self.table.currentRow()
        return self.filtered[row] if 0 <= row < len(self.filtered) else None

    def refresh(self):
        if self.closed:
            return
        root = str(work_root(self.window.store))
        self.book = PlanBook(root)
        selected_id = (self.selected() or {}).get("id")
        self.read_background(lambda context: self.book.scan(None, context),
                             lambda report: self.apply_report(report, selected_id, root))

    def apply_report(self, report, selected_id, root):
        if root != str(work_root(self.window.store)):
            return
        self.items = report["items"]
        self.render()
        callback = getattr(self, "on_items_changed", None)
        if callback:
            callback(self.items)
        if selected_id:
            row = next((i for i, item in enumerate(self.filtered) if item["id"] == selected_id), -1)
            if row >= 0:
                self.table.selectRow(row)
        if self.table.currentRow() < 0 and self.table.rowCount():
            self.table.selectRow(0)
        self.watch([str(Path(root) / "计划")])
        visible_count = sum(item["type"] != "review" for item in self.items)
        review_count = len(self.items) - visible_count
        summary = (f"列表中有 {visible_count} 条长期任务、周期任务或模板；"
                   f"另有 {review_count} 条周月复盘在“周月总结”中。可用上方分类分别查看两种任务。"
                   if self.items else "还没有长期任务、周期任务或模板；可从上方对应按钮新建。")
        self.status.setText(summary +
                            ("\n" + "\n".join(report["errors"][:5]) if report["errors"] else ""))

    def render(self, *_):
        if not hasattr(self, "table"):
            return
        kind = self.kind_filter.tabData(self.kind_filter.currentIndex()) or ""
        query = self.search.text().strip().casefold()
        counts = {
            "plan": sum(item["type"] in {"plan", "goal"} for item in self.items),
            "recurring": sum(item["type"] == "recurring" for item in self.items),
        }
        for index, (category, title) in enumerate(self.CATEGORIES):
            count = sum(item["type"] != "review" for item in self.items) if not category else counts[category]
            self.kind_filter.setTabText(index, f"{title} ({count})")
        selected_id = (self.selected() or {}).get("id")
        self.filtered = [item for item in self.items
                         if item["type"] != "review"
                         and (not kind or (item["type"] in {"plan", "goal"} if kind == "plan"
                                           else item["type"] == kind))
                         and (not query or query in (item["title"] + " " + item["body"] + " "
                             + str(item["metadata"].get("project_id", "")) + " "
                             + str(item["metadata"].get("parent_id", ""))).casefold())]
        self.filtered_count.setText(f"{len(self.filtered)} 条")
        self.table.setRowCount(len(self.filtered))
        for row, item in enumerate(self.filtered):
            metadata = item["metadata"]
            values = (KINDS[item["type"]], item["title"], STATUS_LABELS.get(metadata.get("status", "planned"), "未知"),
                      metadata.get("start", "—") or "—", metadata.get("due", "—") or "—")
            for column, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(str(value))
                if column == 1:
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column, cell)
            self.table.setRowHeight(row, 54)
        if selected_id:
            row = next((i for i, item in enumerate(self.filtered) if item["id"] == selected_id), -1)
            if row >= 0:
                self.table.selectRow(row)
        if self.table.currentRow() < 0 and self.table.rowCount():
            self.table.selectRow(0)
        self.render_detail()

    def render_detail(self):
        item = self.selected()
        if not item:
            self.detail_title.setText("从左侧选择计划查看详情")
            self.detail_hint.show()
            for value in self.detail_meta.values():
                value.setText("—")
            self.detail_body.setMarkdown("选择左侧计划后，这里会显示计划正文。")
            self.edit_button.setEnabled(False)
            self.delete_button.setEnabled(False)
            self.status_button.setEnabled(False)
            self.apply_button.setEnabled(False)
            self.target_day.hide()
            self.apply_button.hide()
            return
        metadata = item["metadata"]
        self.detail_hint.hide()
        self.detail_title.setText(item["title"])
        self.detail_meta["kind"].setText(KINDS[item["type"]])
        self.detail_meta["status"].setText(STATUS_LABELS.get(metadata.get("status", "planned"), "未知"))
        self.detail_meta["start"].setText(metadata.get("start") or "—")
        self.detail_meta["due"].setText(metadata.get("due") or "—")
        self.detail_meta["project_id"].setText(metadata.get("project_id") or "—")
        self.detail_meta["parent_id"].setText(metadata.get("parent_id") or "—")
        frequency_text = FREQUENCY_LABELS.get(metadata.get("frequency"), "—")
        if item["type"] == "recurring" and metadata.get("frequency") == "weekly":
            weekdays = metadata.get("weekdays")
            if not isinstance(weekdays, (list, tuple, set)) or not weekdays:
                try:
                    weekdays = [date.fromisoformat(metadata.get("start", "")).isoweekday()]
                except ValueError:
                    weekdays = []
            names = [WEEKDAY_LABELS[int(day) - 1] for day in weekdays
                     if str(day).isdigit() and 1 <= int(day) <= 7]
            if names:
                frequency_text += "（" + "、".join(names) + "）"
        self.detail_meta["frequency"].setText(frequency_text)
        self.detail_body.setMarkdown(markdown_preserving_linebreaks(item.get("body") or "暂无正文。"))
        self.edit_button.setText("编辑计划")
        self.edit_button.setEnabled(True)
        self.delete_button.setEnabled(True)
        self.status_button.setEnabled(item["type"] != "review")
        self.status_button.setText("重新打开" if metadata.get("status") == "done" else "标记完成")
        self.apply_button.setEnabled(item["type"] == "template")
        self.target_day.setVisible(item["type"] == "template")
        self.apply_button.setVisible(item["type"] == "template")

    def create_item(self):
        self.edit_item(None, "plan")

    def edit_item(self, item=None, kind=None):
        item = item or self.selected()
        kind = item["type"] if item else (kind or "plan")
        dialog = PlanEntryDialog(self, kind, item)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values, body = dialog.values(), dialog.body.toPlainText()
        kind = dialog.kind
        book = self.book
        def save(context):
            context.checkpoint()
            return book.save(kind, values, body, item)
        self.window.submit(None, "保存" + KINDS[kind], save, lambda _: self.refresh(), persist_result=False)

    def delete_item(self):
        item = self.selected()
        if not item:
            return
        answer = QMessageBox.question(
            self, "删除计划",
            f"确定永久删除“{item['title']}”吗？\n\n将删除对应的计划 Markdown 文件，此操作无法撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        book = self.book
        def remove(context):
            context.checkpoint()
            return book.delete(item)
        self.window.submit(None, "删除计划", remove, lambda _: self.refresh(), persist_result=False)

    def toggle_done(self):
        item = self.selected()
        if not item:
            return
        values = {key: item["metadata"].get(key, "") for key in ("title", "start", "due", "project_id", "parent_id", "frequency", "progress")}
        values["status"] = "active" if item["metadata"].get("status") == "done" else "done"
        if values["status"] == "done":
            values["progress"] = 100
        book = self.book
        self.window.submit(None, "更新计划状态", lambda context: book.save(item["type"], values, item["body"], item),
                           lambda _: self.refresh(), persist_result=False)

    def apply_template(self):
        item = self.selected()
        if not item or item["type"] != "template":
            return
        day = self.target_day.date().toPython()
        daily = Daily(work_root(self.window.store), day)
        tasks = []
        for line in item["body"].splitlines():
            value = line.strip()
            if value.startswith("- [ ] "):
                value = value[6:].strip()
            elif value.startswith("- ") or value.startswith("* "):
                value = value[2:].strip()
            if value and not value.startswith("#"):
                tasks.append(value)
        if not tasks:
            QMessageBox.information(self, "模板没有任务", "模板正文中没有可套用的任务行。请添加普通文本或列表项。")
            return
        if any(len(task) > 240 for task in tasks):
            QMessageBox.information(self, "模板任务过长", "每项每日任务最多 240 字，请先缩短模板任务。")
            return
        def apply(context):
            report = daily.load()
            context.checkpoint()
            expected = report["original"]
            text = daily.template() if expected is None else expected
            heading = re.search(r"(?m)^## 今日任务\s*$", text)
            entries = "\n".join("- [ ] " + task for task in tasks)
            if heading:
                following = re.search(r"(?m)^## ", text[heading.end():])
                position = heading.end() + following.start() if following else len(text)
                text = text[:position].rstrip("\r\n") + "\n" + entries + "\n\n" + text[position:]
            else:
                text = text.rstrip("\r\n") + "\n\n## 今日任务\n" + entries + "\n"
            daily.save(text, expected)
            return {"count": len(tasks), "day": day.isoformat()}
        self.window.submit(None, "套用计划模板", apply, lambda result: self.template_applied(result), persist_result=False)

    def template_applied(self, result):
        if result["day"] == date.today().isoformat():
            self.window.today_page.refresh()
        self.status.setText(f"已将 {result['count']} 项任务写入 {result['day']} 的每日 Markdown。")


class PlanSummaryPanel(QWidget):
    """Browse and author weekly/monthly reviews stored as plan Markdown records."""

    def __init__(self, page):
        super().__init__()
        self.page = page
        self.items = []
        self.setObjectName("PlanSummaryPanel")
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        heading = QHBoxLayout()
        title_group = QVBoxLayout()
        title_group.setSpacing(3)
        title = QLabel("周月总结")
        title.setObjectName("PlanManagerTitle")
        description = QLabel("回顾已完成事项、记录问题，并整理下一阶段安排。总结保存在计划 Markdown 中。")
        description.setObjectName("PlanManagerDescription")
        description.setWordWrap(True)
        title_group.addWidget(title)
        title_group.addWidget(description)
        heading.addLayout(title_group, 1)
        self.edit_button = control("编辑总结", self.edit_selected)
        self.edit_button.setEnabled(False)
        heading.addWidget(self.edit_button)
        heading.addWidget(control("＋ 新建周总结", lambda: page.create_review("weekly"), True))
        heading.addWidget(control("＋ 新建月总结", lambda: page.create_review("monthly")))
        root.addLayout(heading)

        self.period_tabs = QTabBar()
        self.period_tabs.setObjectName("PlanSummaryTabs")
        for key, text in (("all", "全部总结"), ("weekly", "周总结"), ("monthly", "月总结")):
            index = self.period_tabs.addTab(text)
            self.period_tabs.setTabData(index, key)
        self.period_tabs.currentChanged.connect(self.render)
        root.addWidget(self.period_tabs)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.list = QListWidget()
        self.list.setObjectName("PlanSummaryList")
        self.list.setMinimumWidth(260)
        self.list.currentRowChanged.connect(self.show_selected)
        self.list.currentRowChanged.connect(lambda row: self.edit_button.setEnabled(row >= 0))
        self.list.itemDoubleClicked.connect(lambda *_: self.edit_selected())
        split.addWidget(self.list)
        self.preview = QTextBrowser()
        self.preview.setObjectName("PlanSummaryPreview")
        self.preview.setOpenExternalLinks(False)
        self.preview.setPlaceholderText("选择一条总结查看正文。")
        split.addWidget(self.preview)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

    @staticmethod
    def period(item):
        value = item["metadata"].get("summary_period")
        if value in {"weekly", "monthly"}:
            return value
        title = item["title"].strip()
        if title.startswith(("月总结", "月度总结")):
            return "monthly"
        if title.startswith(("周总结", "周度总结")):
            return "weekly"
        return "legacy"

    def set_items(self, items):
        self.items = [item for item in items if item["type"] == "review"]
        self.render()

    def render(self, *_):
        selected_id = self.list.currentItem().data(Qt.ItemDataRole.UserRole) if self.list.currentItem() else None
        filter_key = self.period_tabs.tabData(self.period_tabs.currentIndex()) or "all"
        filtered = [item for item in self.items
                    if filter_key == "all" or self.period(item) == filter_key]
        filtered.sort(key=lambda item: (item["metadata"].get("updated", ""), item["title"].casefold()), reverse=True)
        self.list.blockSignals(True)
        self.list.clear()
        selected_row = -1
        for index, item in enumerate(filtered):
            kind = {"weekly": "周总结", "monthly": "月总结"}.get(self.period(item), "复盘")
            changed = item["metadata"].get("updated") or item["metadata"].get("created") or ""
            row = QListWidgetItem(f"{kind}  ·  {item['title']}\n{changed}")
            row.setData(Qt.ItemDataRole.UserRole, item["id"])
            row.setToolTip(item["title"])
            self.list.addItem(row)
            if item["id"] == selected_id:
                selected_row = index
        if self.list.count():
            self.list.setCurrentRow(max(0, selected_row))
        self.list.blockSignals(False)
        self.edit_button.setEnabled(self.list.currentRow() >= 0)
        self.show_selected(self.list.currentRow())
        if not filtered:
            empty = "还没有周月总结。点击右上角新建，记录实际进展。"
            self.preview.setMarkdown(empty)

    def show_selected(self, row):
        if not 0 <= row < self.list.count():
            self.preview.setMarkdown("选择左侧总结查看内容。")
            return
        identity = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        item = next((entry for entry in self.items if entry["id"] == identity), None)
        if not item:
            return
        kind = "周总结" if self.period(item) == "weekly" else "月总结" if self.period(item) == "monthly" else "计划复盘"
        updated = item["metadata"].get("updated") or item["metadata"].get("created") or ""
        body = markdown_preserving_linebreaks(item["body"])
        self.preview.setMarkdown(f"# {item['title']}\n\n**{kind}**　{updated}\n\n---\n\n{body}")

    def edit_selected(self):
        row = self.list.currentRow()
        if not 0 <= row < self.list.count():
            return
        identity = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        item = next((entry for entry in self.items if entry["id"] == identity), None)
        if item:
            self.page.edit_plan_record("review", item)


class PlanningDashboard(QWidget):
    """Planning overview laid out like the product prototype dashboard."""

    def __init__(self, page):
        super().__init__()
        self.setObjectName("PlanningDashboard")
        self.page = page
        self.window = page.window
        self.anchor = date.today()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 18)
        root.setSpacing(12)

        hero = QFrame()
        hero.setObjectName("PlanHero")
        heading = QHBoxLayout(hero)
        heading.setContentsMargins(16, 12, 16, 12)
        heading.setSpacing(11)
        identity = QFrame()
        identity.setObjectName("PlanTitleIcon")
        identity.setFixedSize(46, 46)
        identity_layout = QVBoxLayout(identity)
        identity_layout.setContentsMargins(0, 0, 0, 0)
        glyph = QLabel()
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        glyph.setPixmap(nav_icon(8, "#3979ff").pixmap(25, 25))
        identity_layout.addWidget(glyph)
        heading.addWidget(identity)
        title_group = QVBoxLayout()
        title_group.setSpacing(2)
        title = label("计划", "PlanTitle")
        subtitle = label("把每天的安排与长期任务放在一处，专注执行，高效推进。", "PlanSubtitle")
        title_group.addWidget(title)
        title_group.addWidget(subtitle)
        heading.addLayout(title_group, 1)
        heading.addWidget(control("＋ 添加任务", lambda: self.page.create_shared_task("task", self.anchor), True))
        heading.addWidget(control("刷新", self.page.refresh))
        root.addWidget(hero)

        content = QHBoxLayout()
        content.setSpacing(10)
        self.main_column = QWidget()
        self.main_column.setMinimumWidth(0)
        main_layout = QVBoxLayout(self.main_column)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(9)

        metric_row = QHBoxLayout()
        metric_row.setSpacing(9)
        self.metrics = [
            Metric("今日待办", "#3979ff", 0, "今天的任务与日程"),
            Metric("已完成", "#21a77a", 3, "今天已完成的事项"),
        ]
        for metric, role in zip(self.metrics, ("daily", "projects")):
            metric.setProperty("planMetric", role)
            metric_row.addWidget(metric, 1)
        main_layout.addLayout(metric_row)

        self.day_section = Section("今日计划", 9, "添加日程 ＋", lambda: self.page.create_shared_task("schedule", self.anchor))
        self.day_section.setProperty("planSection", "daily")
        self.day_section.setMinimumWidth(0)
        date_bar = QFrame()
        date_bar.setObjectName("PlanDateBar")
        toolbar = QHBoxLayout(date_bar)
        toolbar.setContentsMargins(8, 5, 8, 5)
        toolbar.setSpacing(6)
        self.day_label = label("", "PlanDayLabel")
        toolbar.addWidget(self.day_label, 1)
        toolbar.addWidget(control("‹", self.previous_period))
        self.date_control = QDateEdit(QDate.currentDate())
        self.date_control.setCalendarPopup(True)
        self.date_control.setDisplayFormat("yyyy年MM月dd日")
        self.date_control.dateChanged.connect(self.change_anchor)
        toolbar.addWidget(self.date_control)
        toolbar.addWidget(control("›", self.next_period))
        toolbar.addWidget(control("今天", self.show_today))
        self.day_section.body.addWidget(date_bar)

        self.day_table = QTableWidget(0, 6)
        self.day_table.setObjectName("PlanDayTable")
        self.day_table.setHorizontalHeaderLabels(["", "类型", "任务 / 安排", "关联项目", "优先级", "完成状态 / 操作"])
        self.day_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.day_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.day_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.day_table.setWordWrap(False)
        self.day_table.setAlternatingRowColors(True)
        self.day_table.verticalHeader().hide()
        day_header = self.day_table.horizontalHeader()
        day_header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        day_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column, width in ((0, 38), (1, 74), (3, 142), (4, 82), (5, 204)):
            self.day_table.setColumnWidth(column, width)
        self.day_table.setMinimumHeight(220)
        self.day_section.body.addWidget(self.day_table, 1)
        self.day_section.title.setText("今日待办")
        main_layout.addWidget(self.day_section, 1)
        content.addWidget(self.main_column, 1)
        root.addLayout(content, 1)
        self.set_day(refresh=False)

    @staticmethod
    def clear_content(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                widget = item.widget()
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout():
                PlanningDashboard.clear_content(item.layout())
                item.layout().deleteLater()

    @staticmethod
    def add_empty(layout, text):
        empty = label(text, "TodayMuted")
        empty.setWordWrap(True)
        layout.addWidget(empty)

    def update_overview(self, rows, items):
        done = sum(bool(item.get("done")) for item in rows)
        for metric, value in zip(self.metrics, (len(rows), done)):
            metric.value.setText(str(value))
        unfinished = sum(1 for item in rows if not item.get("done"))
        self.metrics[0].note.setText(f"所选日期 · {unfinished} 项未完成")
        self.metrics[1].note.setText(f"完成 {done} / {len(rows)} 项")


    def set_day(self, refresh=True):
        if refresh:
            self.page.agenda.set_custom_range(self.anchor, self.anchor)
        else:
            self.page.agenda.anchor_date = self.anchor
            self.page.agenda.mode.blockSignals(True)
            self.page.agenda.mode.setCurrentIndex(MODE_CUSTOM)
            self.page.agenda.mode.blockSignals(False)
            for picker in (self.page.agenda.start, self.page.agenda.end):
                picker.blockSignals(True)
                picker.setDate(QDate(self.anchor.year, self.anchor.month, self.anchor.day))
                picker.blockSignals(False)
            self.page.agenda.refresh_fields()
        self.date_control.blockSignals(True)
        self.date_control.setDate(QDate(self.anchor.year, self.anchor.month, self.anchor.day))
        self.date_control.blockSignals(False)
        self.render_days(self.page.agenda.rows)

    def change_anchor(self, qdate):
        self.anchor = qdate.toPython()
        self.set_day()

    def previous_period(self):
        self.anchor -= timedelta(days=1)
        self.set_day()

    def next_period(self):
        self.anchor += timedelta(days=1)
        self.set_day()

    def show_today(self):
        self.anchor = date.today()
        self.set_day()

    def render_days(self, rows):
        self.day_table.setUpdatesEnabled(False)
        for row in range(self.day_table.rowCount()):
            for column in range(self.day_table.columnCount()):
                widget = self.day_table.cellWidget(row, column)
                if widget:
                    widget.hide()
                    widget.setParent(None)
                    widget.deleteLater()
        day_rows = [entry for entry in rows if entry["day"] == self.anchor.isoformat()]
        day_rows.sort(key=lambda entry: (entry["kind"], entry["title"].casefold()))
        weekdays = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
        self.day_label.setText(f"{self.anchor:%Y年%m月%d日} · {weekdays[self.anchor.weekday()]} · 共 {len(day_rows)} 项安排")
        self.date_control.blockSignals(True)
        self.date_control.setDate(QDate(self.anchor.year, self.anchor.month, self.anchor.day))
        self.date_control.blockSignals(False)
        self.day_table.clearSpans()
        self.day_table.clearContents()
        self.day_table.setEnabled(True)
        self.day_table.setRowCount(len(day_rows) or 1)
        for index, entry in enumerate(day_rows):
            time_text, title_text = self._display_time_title(entry)
            checkbox = QCheckBox()
            can_toggle = entry["kind"] in {"任务", "日程"}
            checkbox.setEnabled(can_toggle)
            checkbox.setChecked(bool(entry["done"]))
            checkbox.setToolTip("勾选状态会保存到统一任务库。" if can_toggle and entry.get("planned_task_id") else
                                "勾选状态会保存到对应日期的每日 Markdown。" if can_toggle else
                                "周期任务的完成状态保存在计划记录中。")
            checkbox.stateChanged.connect(lambda state, value=entry: self.toggle_day_item(value, state == 2))
            check_cell = QWidget()
            check_layout = QHBoxLayout(check_cell)
            check_layout.setContentsMargins(0, 0, 0, 0)
            check_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            check_layout.addWidget(checkbox)
            self.day_table.setCellWidget(index, 0, check_cell)

            kind_cell = label(entry["kind"], "PlanTypeTag")
            kind_cell.setProperty("kind", entry["kind"])
            kind_cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.day_table.setCellWidget(index, 1, kind_cell)
            title_cell = QTableWidgetItem(title_text)
            title_cell.setToolTip(title_text)
            self.day_table.setItem(index, 2, title_cell)
            project_id = str(entry.get("project_id") or "—")
            project_cell = QTableWidgetItem(project_id)
            project_cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            project_cell.setToolTip(project_id if project_id != "—" else "此日计划没有关联项目编号")
            self.day_table.setItem(index, 3, project_cell)
            priority_text = str(entry.get("priority") or "—")
            priority_cell = label(priority_text, "PlanPriorityTag")
            priority_cell.setProperty("priority", "none" if priority_text == "—" else priority_text)
            priority_cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.day_table.setCellWidget(index, 4, priority_cell)

            if entry["kind"] == "周期任务":
                action = QWidget()
                action_layout = QHBoxLayout(action)
                action_layout.setContentsMargins(2, 0, 2, 0)
                status = QComboBox()
                status.addItems(["未完成", "已完成"])
                status.setCurrentIndex(1 if entry["done"] else 0)
                status.currentIndexChanged.connect(
                    lambda state, value=entry: self.toggle_plan_occurrence(value, state == 1))
                action_layout.addWidget(status, 1)
            else:
                action = QWidget()
                action_layout = QHBoxLayout(action)
                action_layout.setContentsMargins(4, 0, 4, 0)
                action_layout.setSpacing(4)
                if entry["kind"] == "日程":
                    status = QComboBox()
                    status.addItems(["未完成", "已完成"])
                    status.setCurrentIndex(1 if entry["done"] else 0)
                    status.setMinimumWidth(98)
                    status.currentIndexChanged.connect(
                        lambda state, value=entry: self.toggle_day_item(value, state == 1))
                else:
                    state_text = "已完成" if entry["done"] else "未完成"
                    status = label(state_text, "TodayMuted")
                action_layout.addWidget(status, 1)
                delete_button = QPushButton("删除")
                delete_button.setProperty("danger", True)
                delete_button.setMinimumWidth(58)
                delete_button.setToolTip("从统一任务库中删除该条目" if entry.get("planned_task_id") else "从这一天的每日 Markdown 中删除该条目")
                delete_button.clicked.connect(
                    lambda checked=False, value=entry: self.delete_day_item(value))
                action_layout.addWidget(delete_button)
            self.day_table.setCellWidget(index, 5, action)
            self.day_table.setRowHeight(index, 52)
        if not day_rows:
            empty = QTableWidgetItem("这一天还没有任务或日程。使用上方按钮添加，周期任务会按规则显示在这里。")
            empty.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.day_table.setItem(0, 2, empty)
            self.day_table.setSpan(0, 2, 1, 4)
            self.day_table.setRowHeight(0, 64)
        self.day_table.setUpdatesEnabled(True)
        self.update_overview(day_rows, self.page.records.items)

    @staticmethod
    def _display_time_title(entry):
        title = str(entry.get("title", ""))
        if entry.get("kind") == "日程":
            title = re.sub(r"^(?:[01]\d|2[0-3]):[0-5]\d(?:\s*[–-]\s*(?:[01]\d|2[0-3]):[0-5]\d)?\s+", "", title)
        return "", title

    def select_day(self, day):
        self.anchor = day
        self.set_day()

    def toggle_day_item(self, entry, done):
        if entry["kind"] not in {"任务", "日程"}:
            return
        if entry.get("planned_task_id"):
            status = "done" if done else "planned"
            def update(context):
                context.checkpoint()
                return self.page.window.store.set_planned_task_status(entry["planned_task_id"], status)
            def saved(_):
                self.page.refresh()
                self.page.window.today_page.refresh()
                self.page.window.project_page.refresh()
            self.page.window.submit(None, "更新统一任务状态", update, saved, persist_result=False)
            return
        day = date.fromisoformat(entry["day"])
        daily = Daily(work_root(self.window.store), day)
        def save(context):
            report = daily.load()
            lines = report["text"].splitlines()
            line = entry["line"]
            if report["original"] is None or not 0 <= line < len(lines):
                raise UserError("每日计划已变化，请刷新后重试。")
            match = re.match(r"^\s*[-*] (?:\[[ xX]\] )?(.*)$", lines[line])
            if not match or match.group(1) != entry["text"]:
                raise UserError("日程或任务内容已变化，请刷新后重试。")
            context.checkpoint()
            return daily.toggle(line, done, report["original"])
        title = "更新日程状态" if entry["kind"] == "日程" else "更新日计划任务"
        self.window.submit(None, title, save,
                           lambda _: self.page.agenda.reload(), persist_result=False)

    def delete_day_item(self, entry):
        if entry["kind"] not in {"任务", "日程"}:
            return
        kind = "日程" if entry["kind"] == "日程" else "任务"
        answer = QMessageBox.question(
            self, f"删除{kind}", f"确定从每日计划中删除“{entry['title']}”？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if entry.get("planned_task_id"):
            def remove(context):
                context.checkpoint()
                self.page.window.store.delete_planned_task(entry["planned_task_id"])
            def removed(_):
                self.page.refresh()
                self.page.window.today_page.refresh()
                self.page.window.project_page.refresh()
            self.page.window.submit(None, "删除统一任务", remove, removed, persist_result=False)
            return
        day = date.fromisoformat(entry["day"])
        daily = Daily(work_root(self.window.store), day)
        def remove(context):
            report = daily.load()
            context.checkpoint()
            return daily.remove_entry(entry["line"], entry["text"], report["original"])
        self.window.submit(None, f"删除日计划{kind}", remove,
                           lambda _: self.page.agenda.reload(), persist_result=False)

    def toggle_plan_occurrence(self, entry, done):
        item = next((record for record in self.page.records.items
                     if record["id"] == entry.get("plan_id") and record["type"] == "recurring"), None)
        if not item:
            self.page.records.refresh()
            return
        day = date.fromisoformat(entry["day"])
        book = self.page.records.book
        def save(context):
            context.checkpoint()
            return book.set_occurrence_done(item, day, done)
        def saved(_result):
            self.page.records.refresh()
            self.page.agenda.reload()
        self.window.submit(None, "更新周期任务状态", save, saved, persist_result=False)

    def set_plan_records(self, _items):
        self.update_overview(self.page.agenda.rows, _items)

    def create_goal(self):
        self.page.create_plan("plan")

    def show_records(self, kind):
        self.page.open_record_manager(kind)


class PlanningPage(QWidget):
    def __init__(self, window, today_page):
        super().__init__()
        self.setObjectName("PlanningPage")
        self.window = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.agenda = AgendaPanel(window, today_page, self)
        self.agenda.mode.setCurrentIndex(MODE_CUSTOM)
        self.agenda.completed.blockSignals(True)
        self.agenda.completed.setChecked(True)
        self.agenda.completed.blockSignals(False)
        self.records = PlanRecordsPanel(window)
        self.records.setParent(self)
        self.dashboard = PlanningDashboard(self)
        self.dashboard_scroll = QScrollArea()
        self.dashboard_scroll.setWidgetResizable(True)
        self.dashboard_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.dashboard_scroll.setWidget(self.dashboard)
        self.agenda.on_rows_changed = self.dashboard.render_days
        self.records.on_items_changed = self.plan_records_changed

        self.section_tabs = QTabBar()
        self.section_tabs.setObjectName("PlanningSections")
        for title in ("今日待办", "目标与规划", "周月总结"):
            self.section_tabs.addTab(title)
        layout.addWidget(self.section_tabs)

        self.views = QStackedWidget()
        self.views.setObjectName("PlanningViews")
        self.views.addWidget(self.dashboard_scroll)
        self.planning_view = QWidget()
        planning_layout = QVBoxLayout(self.planning_view)
        planning_layout.setContentsMargins(0, 0, 0, 0)
        planning_layout.setSpacing(9)
        self.timeline_section = Section("长期规划时间线", 3, "查看全部 →", self.open_timeline_dialog)
        self.timeline_section.setProperty("planSection", "timeline")
        self.timeline_widget = PlanTimelineWidget()
        self.timeline_section.body.addWidget(self.timeline_widget, 1)
        planning_layout.addWidget(self.records, 1)
        planning_layout.addWidget(self.timeline_section)
        self.views.addWidget(self.planning_view)
        self.summary_panel = PlanSummaryPanel(self)
        self.views.addWidget(self.summary_panel)
        layout.addWidget(self.views, 1)
        self.section_tabs.currentChanged.connect(self.views.setCurrentIndex)
        self.dashboard.set_plan_records(self.records.items)
        self.dashboard.render_days(self.agenda.rows)

    def open_record_manager(self, kind=None):
        self.records.select_kind(kind or "")
        self.records.refresh()
        self.section_tabs.setCurrentIndex(1)

    def show_dashboard(self):
        self.section_tabs.setCurrentIndex(0)

    def show_planning(self):
        self.section_tabs.setCurrentIndex(1)

    def show_summaries(self):
        self.section_tabs.setCurrentIndex(2)

    def open_timeline_dialog(self):
        PlanTimelineDialog(self).exec()

    def edit_plan(self, item):
        self.edit_plan_record(item["type"], item)

    def create_plan(self, kind):
        self.edit_plan_record(kind)

    def create_review(self, period):
        self.show_summaries()
        self.edit_plan_record("review", summary_period=period)

    def edit_plan_record(self, kind, item=None, on_saved=None, summary_period=None):
        dialog = PlanEntryDialog(self, kind, item, summary_period)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values, body = dialog.values(), dialog.body.toPlainText()
        book = self.records.book
        selected_id = item.get("id") if item else None
        root = str(work_root(self.window.store))
        def save(context):
            book.save(kind, values, body, item)
            return book.scan(context=context)
        def saved(report):
            self.records.apply_report(report, selected_id, root)
            self.agenda.refresh()
            if on_saved:
                on_saved()
        self.window.submit(None, "保存" + KINDS[kind],
                           save, saved, persist_result=False)

    def create_shared_task(self, kind="task", day=None, project_id=""):
        dialog = UnifiedTaskDialog(self.window, kind=kind, due_date=day, project_id=project_id)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        def save(context):
            context.checkpoint()
            return self.window.store.add_planned_task(values)
        def saved(_task):
            self.refresh()
            self.window.today_page.refresh()
            self.window.project_page.refresh()
        self.window.submit(None, "新建统一任务", save, saved, persist_result=False)

    def plan_records_changed(self, items):
        self.dashboard.set_plan_records(items)
        self.timeline_widget.set_items(items)
        self.summary_panel.set_items(items)

    def refresh(self):
        self.agenda.refresh()
        self.records.refresh()

    def stop_updates(self):
        self.agenda.stop_updates()
        self.records.stop_updates()
