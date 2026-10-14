"""Project dashboard widgets for shared tasks, user materials, and Markdown summaries."""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt, QRect, QPoint, QRectF
from PySide6.QtGui import QColor, QPainter, QLinearGradient, QPainterPath, QPolygonF, QPen, QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout,
    QTextBrowser, QSizePolicy, QStyle, QFrame, QLabel, QPushButton, QGridLayout,
    QCheckBox, QScrollArea)

from ..workbench import frontmatter
from ..project_workspaces import STATES
from .components import nav_icon
from .design import tokens, ui_color
from .today_widgets import Metric, TodayDelegate, COLORS, ROW_DATA, label, link


def outside_fences(text):
    """Keep line positions while ignoring fenced examples in counts and section parsing."""
    marker, length = "", 0
    for line in text.splitlines(keepends=True):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if marker:
            if fence and fence[1][0] == marker and len(fence[1]) >= length and not fence[2].strip():
                marker = ""
            yield "\n" if line.endswith("\n") else ""
        elif fence:
            marker, length = fence[1][0], len(fence[1])
            yield "\n" if line.endswith("\n") else ""
        else:
            yield line


def sections(text, level=2):
    """Return original level-two sections, including Markdown and nested headings."""
    _, body = frontmatter(text)
    original = body.splitlines(keepends=True)
    starts = [(i, m[1].strip()) for i, line in enumerate(outside_fences(body))
              if (m := re.match(r"^" + "#" * level + r" (.+?)\s*#*\s*$", line))]
    return [(name, "".join(original[start + 1:starts[i + 1][0] if i + 1 < len(starts) else len(original)]).strip())
            for i, (start, name) in enumerate(starts)]


def task_snapshot(text):
    """Count explicit tasks and classify them by their TASKS.md heading."""
    _, body = frontmatter(text)
    lines = list(outside_fences(body))
    tasks = []
    header = None
    source = "manual"
    done_states = {"done", "completed", "complete", "完成", "已完成"}
    for line in lines:
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if heading:
            if len(heading[1]) <= 2:
                title = re.sub(r"[\s_-]+", "", heading[2]).casefold()
                if (("ai" in title or "agent" in title) and any(word in title for word in ("规划", "计划", "生成"))):
                    source = "ai"
                elif any(word in title for word in ("手动", "手工", "人工", "用户编写")):
                    source = "manual"
                else:
                    source = "manual"
            header = None
            continue
        check = re.match(r"^\s*[-*+] \[([ xX])\]\s+(.+)", line)
        if check:
            is_done = check[1].casefold() == "x"
            tasks.append({"text": check[2].strip(), "done": is_done,
                          "status": "completed" if is_done else "planned", "source": source})
            continue
        if not line.strip().startswith("|"):
            header = None
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if "状态" in cells and any(key in cells for key in ("动作", "任务")):
            header = (cells.index("状态"), cells.index("动作" if "动作" in cells else "任务"))
        elif (header and max(header) < len(cells) and cells[header[1]]
                and not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)):
            state = cells[header[0]].casefold()
            tasks.append({"text": cells[header[1]], "done": state in done_states,
                          "status": state, "source": source})
    done_count = sum(task["done"] for task in tasks)
    active_count = sum(task["status"] in {"in_progress", "active", "进行中", "处理中"} for task in tasks)
    overdue_count = sum(task["status"] in {"overdue", "late", "逾期", "已逾期"} for task in tasks)
    pending_count = max(0, len(tasks) - done_count - active_count - overdue_count)
    return {"tasks": tasks, "ai_tasks": [task for task in tasks if task["source"] == "ai"],
            "manual_tasks": [task for task in tasks if task["source"] == "manual"],
            "total": len(tasks), "done": done_count,
            "in_progress": active_count, "pending": pending_count, "overdue": overdue_count,
            "progress": round(done_count / len(tasks) * 100) if tasks else 0}


def overview_snapshot(status, tasks, handoff, summaries):
    metadata, _ = frontmatter(status)
    blocks = sections(status)
    def find(words, default):
        return "\n\n".join(body for name, body in blocks if any(word in name for word in words)) or default
    risks = []
    for line in outside_fences(status):
        if re.match(r"^\s*[-*+]\s+(?:\*\*)?(?:阻塞(?:/待决定)?|风险|待确认|待决策)[：:]", line):
            risks.append(line.strip())
    entries = [(title, body) for title, body in sections(handoff) if re.search(r"\d{4}-\d{2}-\d{2}", title)]
    checklist = task_snapshot(tasks)
    pending = ["- " + t["text"] for t in checklist["tasks"] if not t["done"]]
    latest_summary = summaries[-1] if summaries else ""
    brief = [(title, body) for title, body in sections(latest_summary, 3)
             if title in {"完成内容", "验证结果", "下一阶段计划"} and body.strip()]
    summary = "\n\n".join("**" + title + "**\n\n" + body for title, body in brief)
    if not summary:
        summary = re.sub(r"(?m)^## .+\n|^\*\*阶段总结：.+\n|^阶段：.+\n", "", latest_summary).strip()
    progress = find(("进度", "当前状态", "进展"), "尚未填写进展，完整记录可查看状态原文。")
    progress = re.sub(r"(?m)(生命周期[：:]\s*)([a-z]+)", lambda m: m[1] + STATES.get(m[2], m[2]), progress)
    recent = []
    for title, body in reversed(entries[-3:]):
        first = next((line.strip().lstrip("- ") for line in body.splitlines()
                      if line.strip() and not re.match(r"^\s*#{1,6}\s", line)), "")
        display_title = re.sub(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}):\d{2}(?:[+-]\d{2}:\d{2}|Z)?", r"\1 \2", title)
        recent.append("- **" + display_title + "**\n\n  " + first[:100])
    return {
        "goals": find(("目标", "验收"), "尚未填写目标与验收。可编辑当前状态补充。"),
        "progress": progress,
        "risks": find(("风险", "阻塞", "待确认", "待决策"), "\n".join(risks) or "状态文件中尚未单独登记风险或待确认项。"),
        "next": "\n".join(pending[:6]) or ("清单任务均已勾选完成。请核对下一步。" if checklist["total"] else "当前任务文件是任务入口，或尚未登记可识别的清单。请打开“任务”查看原记录。"),
        "summary": summary or "还没有阶段总结。点击“写阶段总结”，记录本阶段的完成内容、验证和下一阶段计划。",
        "recent": "\n\n".join(recent) or "还没有按日期追加的日志，点击“写日志”记录最近工作。",
        "updated": str(metadata.get("updated") or "未注明"), "logs": len(entries), "checklist": checklist,
    }


def milestone_rows(text):
    """Extract milestone rows from the user's Markdown table."""
    rows, headers = [], None
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip().startswith("|"):
            headers = None
            continue
        cells = [cell.strip().replace("\\|", "|") for cell in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        normalized = [re.sub(r"[*`_ ]", "", value) for value in cells]
        name_index = next((i for i, value in enumerate(normalized) if value in {"里程碑", "名称", "标题"}), None)
        date_index = next((i for i, value in enumerate(normalized) if value in {"目标日期", "日期", "截止日期"}), None)
        status_index = next((i for i, value in enumerate(normalized) if value == "状态"), None)
        if name_index is not None:
            headers = (status_index, name_index, date_index)
        elif headers and len(cells) > headers[1] and cells[headers[1]]:
            status, name, due = headers
            rows.append({"name": cells[name], "status": cells[status] if status is not None and status < len(cells) else "",
                         "date": cells[due] if due is not None and due < len(cells) else "",
                         "line_number": line_number, "status_column": status,
                         "status_editable": status is not None and status < len(cells)})
    return rows


class ProjectCardDelegate(TodayDelegate):
    def __init__(self, parent):
        super().__init__(parent, "project")
        self.row_height = 112

    def paint(self, painter, option, index):
        rect = option.rect.adjusted(4, 3, -4, -3)
        data = index.data(ROW_DATA) or {}
        painter.save()
        painter.setClipRect(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(ui_color("accent" if selected else "border"))
        painter.setBrush(ui_color("accent_soft" if selected else "surface"))
        painter.drawRoundedRect(rect, 10, 10)
        color = COLORS[data.get("position", 0) % len(COLORS)]
        tile = QRect(rect.x() + 12, rect.y() + 25, 38, 38)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ui_color(color))
        painter.drawRoundedRect(tile, 10, 10)
        painter.drawPixmap(tile.x() + 9, tile.y() + 9, nav_icon(3, "#ffffff").pixmap(20, 20))
        x, width = rect.x() + 61, rect.width() - 72
        t = tokens()
        for dy, text, ink, bold, size in (
            (7, data.get("id", ""), t["muted"], False, 10),
            (25, data.get("name", ""), t["text"], True, 14),
            (47, data.get("summary") or data.get("state_label", ""), color, False, 11)):
            self.text(painter, QRect(x, rect.y() + dy, width, 20), text, ink, bold, size)
        progress = data.get("progress")
        if isinstance(progress, (int, float)) and not isinstance(progress, bool):
            bar = QRect(x, rect.bottom() - 24, max(20, width - 42), 5)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ui_color("border"))
            painter.drawRoundedRect(bar, 3, 3)
            painter.setBrush(ui_color("#3979ff"))
            painter.drawRoundedRect(QRect(bar.x(), bar.y(), int(bar.width() * max(0, min(100, progress)) / 100), bar.height()), 3, 3)
            self.text(painter, QRect(bar.right() + 6, bar.y() - 6, 35, 17), f"{int(progress)}%", t["muted"], False, 9)
        self.text(painter, QRect(x, rect.bottom() - 17, width, 15), data.get("date_label") or "点击继续工作", tokens()["muted"], False, 9)
        painter.restore()


class ProjectMetric(Metric):
    def __init__(self, title, color, icon):
        super().__init__(title, color, icon, "")
        self.layout().setContentsMargins(12, 10, 12, 10)
        self.icon.setFixedSize(32, 32)
        self.value.setObjectName("ProjectMetricValue")
        self.value.setWordWrap(True)
        self.value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(80)
        self.setMinimumHeight(64)


class ProjectMetrics(QWidget):
    def __init__(self, metrics):
        super().__init__()
        self.metrics = metrics
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        for metric in metrics:
            row.addWidget(metric, 1)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        compact = self.width() < 700
        for metric in self.metrics:
            metric.icon.setVisible(not compact)
            metric.note.setVisible(not compact)


class ProjectHero(QFrame):
    """Theme-aware project banner with a soft, vector-drawn mountain scene."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cover_path = ""
        self._cover = QPixmap()

    def set_cover(self, path=""):
        self.cover_path = str(path or "")
        self._cover = QPixmap(self.cover_path) if self.cover_path else QPixmap()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        t = tokens()
        clip = QPainterPath()
        clip.addRoundedRect(rect, 14, 14)
        painter.setClipPath(clip)
        if not self._cover.isNull():
            scaled = self._cover.scaled(rect.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                        Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(rect, scaled, QRect((scaled.width()-rect.width())//2,
                                                   (scaled.height()-rect.height())//2,
                                                   rect.width(), rect.height()))
            shade = QColor("#f4f8ff"); shade.setAlpha(188); painter.fillRect(rect, shade)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0, QColor(t["hero_start"]))
        gradient.setColorAt(1, QColor(t["hero_end"]))
        if self._cover.isNull():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawRoundedRect(rect, 14, 14)
        else:
            tint = QColor(t["hero_start"]); tint.setAlpha(72); painter.fillRect(rect, tint)
        painter.setPen(Qt.PenStyle.NoPen)
        ridge = QColor("#6a9fe9"); ridge.setAlpha(48); painter.setBrush(ridge)
        painter.drawPolygon(QPolygonF([QPoint(rect.right() - 390, rect.bottom()), QPoint(rect.right() - 230, rect.top() + 5),
                                       QPoint(rect.right() - 140, rect.top() + 75), QPoint(rect.right() - 45, rect.top() + 25),
                                       QPoint(rect.right() + 10, rect.bottom())]))
        ridge = QColor("#8bb4ed"); ridge.setAlpha(35); painter.setBrush(ridge)
        painter.drawPolygon(QPolygonF([QPoint(rect.right() - 280, rect.bottom()), QPoint(rect.right() - 125, rect.top() + 52),
                                       QPoint(rect.right() - 25, rect.top() + 105), QPoint(rect.right() + 40, rect.top() + 76),
                                       QPoint(rect.right() + 80, rect.bottom())]))
        snow = QColor("#ffffff"); snow.setAlpha(185); painter.setBrush(snow)
        painter.drawPolygon(QPolygonF([QPoint(rect.right()-230, rect.top()+5), QPoint(rect.right()-188, rect.top()+57),
                                       QPoint(rect.right()-151, rect.top()+35), QPoint(rect.right()-123, rect.top()+68)]))
        painter.drawPolygon(QPolygonF([QPoint(rect.right()-126, rect.top()+51), QPoint(rect.right()-82, rect.top()+105),
                                       QPoint(rect.right()-44, rect.top()+85), QPoint(rect.right()-24, rect.top()+114)]))
        painter.end()
        super().paintEvent(event)

    def fit(self):
        if self.layout():
            self.setMinimumHeight(max(210, self.layout().totalHeightForWidth(self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()


class MilestoneRail(QWidget):
    def __init__(self, color, last=False, parent=None):
        super().__init__(parent)
        self.color, self.last = color, last
        self.setFixedWidth(18)
        self.setMinimumHeight(38)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#dce6f5"), 2))
        if not self.last:
            painter.drawLine(9, 10, 9, self.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.color))
        painter.drawEllipse(4, 4, 11, 11)
        painter.end()


class ProjectOverview(QWidget):
    """Project dashboard made from the selected project's existing records."""
    def __init__(self, open_tasks, show_resources, edit_info, open_documents, open_milestones, toggle_task,
                 toggle_milestone, run_ai_task, open_document, open_resource, upload_document):
        super().__init__()
        self.toggle_task = toggle_task
        self.toggle_milestone = toggle_milestone
        self.run_ai_task = run_ai_task
        self.open_document = open_document
        self.open_resource = open_resource
        self.open_documents = open_documents
        self.upload_document_button = None
        self.upload_resource_button = None
        self.setObjectName("ProjectOverview")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(9)
        root.addLayout(self.grid)
        self.progress_card, self.progress_body = self.card("项目进度", 9)
        self.milestones_card, self.milestones_body = self.card(
            "里程碑", 15, "查看全部 →", open_milestones, scrollable=True)
        self.info_card, self.info_body = self.card("项目信息", 4, "编辑", edit_info)
        self.ai_tasks_title = label("AI 规划（0）", "TodaySectionTitle")
        self.ai_tasks_title.setObjectName("ProjectTaskCardTitle")
        self.ai_tasks_title.setProperty("taskSource", "ai")
        self.ai_tasks_card, self.ai_tasks_body = self.card(
            "AI 规划任务", 9, title_widget=self.ai_tasks_title, scrollable=True)
        self.ai_tasks_body.setSpacing(3)
        self.manual_tasks_title = label("手动编写（0）", "TodaySectionTitle")
        self.manual_tasks_title.setObjectName("ProjectTaskCardTitle")
        self.manual_tasks_title.setProperty("taskSource", "manual")
        self.manual_tasks_card, self.manual_tasks_body = self.card(
            "手动编写任务", 9, "＋ 新建任务", open_tasks,
            title_widget=self.manual_tasks_title, scrollable=True)
        self.manual_tasks_body.setSpacing(3)
        self.documents_card, self.documents_body = self.card("项目资料", 10, "＋ 上传文件", upload_document)
        self.documents_body.setSpacing(5)
        self.resources_card, self.resources_body = self.card("项目资源", 6, "＋ 上传资源", show_resources)
        self.activity_card, self.activity_body = self.card("项目动态", 3, "查看全部 →", open_documents)
        for row, column, span, card in (
            (0, 0, 1, self.progress_card), (0, 1, 1, self.milestones_card),
            (0, 2, 1, self.info_card), (1, 0, 1, self.ai_tasks_card),
            (1, 1, 1, self.manual_tasks_card), (2, 0, 1, self.documents_card),
            (2, 1, 1, self.resources_card), (2, 2, 1, self.activity_card),
        ):
            self.grid.addWidget(card, row, column, 2 if card is self.info_card else 1, span)
        for column in range(3):
            self.grid.setColumnStretch(column, 1)
        for row in range(3):
            self.grid.setRowStretch(row, 1)

    def card(self, title, icon, action=None, callback=None, title_widget=None, scrollable=False):
        frame = QFrame()
        frame.setObjectName("ProjectOverviewCard")
        frame.setMinimumWidth(0)
        frame.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        glyph = QLabel()
        glyph.setPixmap(nav_icon(icon, "#2879f6").pixmap(17, 17))
        heading.addWidget(glyph)
        heading.addWidget(title_widget or label(title, "TodaySectionTitle"), 1)
        if action:
            button = link(action, callback)
            heading.addWidget(button)
            if title == "项目资料":
                self.upload_document_button = button
            elif title == "项目资源":
                self.upload_resource_button = button
        layout.addLayout(heading)
        body_widget = QWidget()
        body = QVBoxLayout(body_widget)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        if scrollable:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.viewport().setAutoFillBackground(False)
            scroll.viewport().setStyleSheet("background: transparent;")
            body_widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            scroll.setWidget(body_widget)
            layout.addWidget(scroll, 1)
        else:
            layout.addWidget(body_widget, 1)
        return frame, body

    @staticmethod
    def clear(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                ProjectOverview.clear(item.layout())
                item.layout().deleteLater()

    @staticmethod
    def empty(layout, text):
        view = label(text, "TodayMuted")
        view.setWordWrap(True)
        layout.addWidget(view)

    @staticmethod
    def _progress_ring(value):
        class Ring(QWidget):
            def __init__(self, amount):
                super().__init__()
                self.amount = amount
                self.setFixedSize(112, 112)

            def paintEvent(self, event):
                painter = QPainter(self)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                box = QRectF(9, 9, 94, 94)
                pen = QPen(ui_color("border"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                painter.setPen(pen); painter.drawArc(box, 0, 360 * 16)
                if self.amount:
                    pen = QPen(ui_color("accent"), 8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                    painter.setPen(pen); painter.drawArc(box, 90 * 16, -int(360 * 16 * max(0, min(100, self.amount)) / 100))
                painter.setPen(ui_color("text")); painter.setFont(painter.font())
                font = painter.font(); font.setPixelSize(18); font.setBold(True); painter.setFont(font)
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, f"{self.amount}%")
                painter.end()
        return Ring(value)

    def set_data(self, snapshot, project, project_info, resources, agents, milestones=None, milestones_exists=False,
                 project_documents=None, milestones_text=""):
        checklist = snapshot["checklist"]
        self.clear(self.progress_body)
        progress = checklist["progress"]
        summary = QHBoxLayout(); summary.setSpacing(10)
        summary.addWidget(self._progress_ring(progress), 0, Qt.AlignmentFlag.AlignVCenter)
        stats = QVBoxLayout(); stats.setSpacing(9)
        for title, value, color in (("已完成", checklist["done"], "#14aa72"),
                                    ("进行中", checklist["in_progress"], "#2879f6"),
                                    ("待开始", checklist["pending"], "#8795ad"),
                                    ("已逾期", checklist["overdue"], "#ed5361")):
            line = QHBoxLayout(); dot = QLabel("●"); dot.setStyleSheet(f"color:{color};")
            line.addWidget(dot); line.addWidget(label(title, "TodayMuted"), 1); line.addWidget(label(str(value), "PlanSummaryItem"))
            stats.addLayout(line)
        summary.addLayout(stats, 1)
        self.progress_body.addLayout(summary)

        self.clear(self.milestones_body)
        milestone_items = milestones or []
        if milestone_items:
            can_edit_milestones = project.get("state") not in {"archived", "unknown"}
            for index, item in enumerate(milestone_items):
                line = QHBoxLayout(); line.setSpacing(8)
                done = item["status"].casefold() in {"已完成", "完成", "completed", "done"}
                color = "#16a978" if done else "#2879f6"
                rail = MilestoneRail(color, index == len(milestone_items) - 1)
                line.addWidget(rail)
                details = QVBoxLayout(); details.setSpacing(2)
                title_row = QHBoxLayout(); title_row.setSpacing(5)
                check = QCheckBox()
                check.setObjectName("ProjectMilestoneCheck")
                check.setChecked(done)
                check.setEnabled(can_edit_milestones and item.get("status_editable", False))
                check.setAccessibleName("完成里程碑：" + item["name"])
                check.setToolTip("取消完成后状态将设为“待开始”。" if done else "标记此里程碑为已完成。")
                def change_milestone_status(checked, row=dict(item), source=milestones_text,
                                            control=check, initial_done=done):
                    if self.toggle_milestone(row, checked, source):
                        control.setEnabled(False)
                    else:
                        control.blockSignals(True)
                        control.setChecked(initial_done)
                        control.blockSignals(False)
                check.toggled.connect(change_milestone_status)
                title_row.addWidget(check, 0, Qt.AlignmentFlag.AlignTop)
                name = label(item["name"], "PlanSummaryItem")
                name.setWordWrap(True)
                title_row.addWidget(name, 1)
                details.addLayout(title_row)
                detail_text = " · ".join(value for value in (item.get("status", ""), item.get("date", "")) if value)
                if detail_text: details.addWidget(label(detail_text, "TodayMuted"))
                line.addLayout(details, 1)
                self.milestones_body.addLayout(line)
            self.milestones_body.addStretch(1)
        else:
            self.empty(self.milestones_body, "里程碑由你在 Obsidian 的项目文档中维护。" if milestones_exists else "还没有里程碑文档。创建空白文档后可在 Obsidian 编辑。")
        self.clear(self.info_body)
        members = project_info.get("members", [])
        member_text = "、".join(str(name) for name in members if isinstance(name, str)) if isinstance(members, list) else ""
        fields = (
            ("项目负责人", project_info.get("owner") or "未设置"),
            ("项目成员", member_text or "未设置成员"),
            ("开始日期", project_info.get("start_date") or "未设置"),
            ("结束日期", project_info.get("end_date") or "未设置"),
            ("优先级", {"high": "高", "medium": "中", "low": "低"}.get(project_info.get("priority", ""), "未设置")),
            ("项目状态", "已归档" if project.get("state") == "archived" else "进行中"),
        )
        info_grid = QGridLayout()
        info_grid.setContentsMargins(0, 0, 0, 0)
        info_grid.setHorizontalSpacing(8)
        info_grid.setVerticalSpacing(8)

        def add_info_field(title, value, row, column, column_span=1):
            field = QFrame()
            field.setObjectName("ProjectInfoField")
            field_layout = QVBoxLayout(field)
            field_layout.setContentsMargins(10, 8, 10, 8)
            field_layout.setSpacing(3)
            caption = label(title, "TodayMuted")
            caption.setObjectName("ProjectInfoLabel")
            value_label = label(value, "PlanSummaryItem")
            value_label.setObjectName("ProjectInfoValue")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            field_layout.addWidget(caption)
            field_layout.addWidget(value_label)
            info_grid.addWidget(field, row, column, 1, column_span)

        for index, (title, value) in enumerate(fields):
            add_info_field(title, value, index // 2, index % 2)
        repository = project_info.get("repository") or (
            "本地 Git 仓库" if (Path(project.get("directory", "")) / ".git").exists() else "未关联")
        add_info_field("关联仓库", repository, 3, 0, 2)
        self.info_body.addLayout(info_grid, 1)
        tags = project_info.get("tags", [])
        tag_text = "  ".join("#" + str(tag) for tag in tags if isinstance(tag, str)) if isinstance(tags, list) else ""
        add_info_field("项目标签", tag_text or "暂无标签", 4, 0, 2)
        tools = [item["name"] for item in resources if item["type"] in {"tool", "website"}]
        add_info_field("相关工具", "、".join(tools[:4]) if tools else "未关联工具", 5, 0, 2)
        for row in range(6):
            info_grid.setRowStretch(row, 1)

        self.clear(self.ai_tasks_body)
        self.clear(self.manual_tasks_body)
        ai_tasks = checklist.get("ai_tasks", [])
        manual_tasks = checklist.get("manual_tasks", checklist["tasks"])
        self.ai_tasks_title.setText(f"AI 规划（{len(ai_tasks)}）")
        self.manual_tasks_title.setText(f"手动编写（{len(manual_tasks)}）")
        self._render_task_group(self.ai_tasks_body, ai_tasks, "暂无 AI 规划任务", "AI 规划任务在此只读展示；已完成项可查看完成记录。", "ai")
        self._render_task_group(self.manual_tasks_body, manual_tasks, "暂无手动任务", "在项目 agent/TASKS.md 的“手动任务”章节登记。", "manual")

        self.clear(self.documents_body)
        material_rows = project_documents or []
        for document in material_rows[:3]:
            row = QFrame()
            row.setObjectName("ProjectMaterialRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 5, 9, 5)
            row_layout.setSpacing(9)
            suffix = Path(document["name"]).suffix.lstrip(".").upper()
            badge = QLabel(suffix[:4] if suffix else "FILE")
            badge.setObjectName("ProjectMaterialType")
            badge.setProperty("fileKind", "markdown" if suffix.casefold() == "md" else "file")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFixedSize(32, 32)
            row_layout.addWidget(badge)
            text_column = QVBoxLayout()
            text_column.setContentsMargins(0, 0, 0, 0)
            text_column.setSpacing(1)
            name = link(document["name"], lambda _=False, path=document["relative"]: self.open_document(path))
            name.setObjectName("ProjectMaterialName")
            name.setToolTip("打开项目资料：" + document["relative"])
            text_column.addWidget(name)
            details = []
            if suffix:
                details.append("Markdown" if suffix.casefold() == "md" else suffix)
            try:
                size = max(0, int(document.get("size", 0)))
            except (TypeError, ValueError):
                size = 0
            if size:
                details.append(f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / (1024 * 1024):.1f} MB")
            if document.get("modified"):
                details.append(str(document["modified"])[:10])
            if details:
                meta = label(" · ".join(details), "ProjectMaterialMeta")
                text_column.addWidget(meta)
            row_layout.addLayout(text_column, 1)
            chevron = QLabel("›")
            chevron.setObjectName("ProjectMaterialChevron")
            row_layout.addWidget(chevron)
            self.documents_body.addWidget(row)
        if not material_rows:
            self.empty(self.documents_body, "这里用于整理你维护的项目资料。上传后会复制到项目的“文档”目录，原文件保持不动。")
        footer = QHBoxLayout()
        footer.setContentsMargins(2, 2, 2, 0)
        footer.addWidget(label(f"共 {len(material_rows)} 个文件", "ProjectMaterialCount"), 1)
        if self.open_documents:
            footer.addWidget(link("查看全部 →", self.open_documents))
        self.documents_body.addLayout(footer)
        self.documents_body.addStretch(1)

        self.clear(self.resources_body)
        for item in resources[:4]:
            button = link(f"▰  {item['name']}", lambda _=False, identity=item["id"]: self.open_resource(identity))
            button.setToolTip("在资源库中打开此条目")
            button.setStyleSheet("text-align:left;")
            self.resources_body.addWidget(button)
        if not resources:
            self.empty(self.resources_body, "还没有关联资源。可从资源库关联当前项目。")

        self.clear(self.activity_body)
        recent = snapshot["recent"]
        entries = re.findall(r"- \*\*(.*?)\*\*\s*\n\s*(.*?)(?=\n\s*- \*\*|\Z)", recent, re.S)
        if entries:
            for title, detail in entries[:4]:
                row = QVBoxLayout(); row.setSpacing(1)
                heading = label(title.strip(), "PlanSummaryItem"); heading.setWordWrap(True); row.addWidget(heading)
                body = label(re.sub(r"\s+", " ", detail).strip()[:120], "TodayMuted"); body.setWordWrap(True); row.addWidget(body)
                self.activity_body.addLayout(row, 1)
        else:
            self.empty(self.activity_body, recent)
        agent_names = agents or []
        self.activity_body.addWidget(label("关联 Agent：" + ("、".join(agent_names) if agent_names else "未设置"), "TodayMuted"))

    def _render_task_group(self, layout, tasks, empty_text, hint, source):
        if not tasks:
            view = label(empty_text, "ProjectTaskEmpty")
            view.setWordWrap(True)
            view.setToolTip(hint)
            layout.addWidget(view)
            return
        for task in tasks:
            row = QHBoxLayout(); row.setSpacing(8)
            title = str(task.get("text") or task.get("title") or "（无标题任务）").strip()
            view = label(title, "ProjectTaskText")
            view.setWordWrap(True)
            view.setMinimumHeight(18)
            view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            view.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            view.setToolTip(title)
            content = QVBoxLayout(); content.setSpacing(2)
            content.addWidget(view)
            done = task.get("status") == "done" or task["done"]
            running = bool(task.get("running"))
            if done:
                completion_note = str(task.get("completion_note", "")).strip() if source == "ai" else ""
                if completion_note:
                    compact_note = completion_note.replace("\n", " · ")
                    if len(compact_note) > 220:
                        compact_note = compact_note[:217].rstrip() + "…"
                    record = label("完成记录：" + compact_note, "TodayMuted")
                    record.setWordWrap(True)
                    record.setToolTip(completion_note)
                    content.addWidget(record)
            elif source == "ai" and running:
                content.addWidget(label("AI 正在执行", "TodayMuted"))
            if task.get("due_date"):
                content.addWidget(label(task["due_date"], "TodayMuted"))
            row.addLayout(content, 1)
            row.setAlignment(content, Qt.AlignmentFlag.AlignTop)
            check = QCheckBox()
            check.setChecked(bool(task["done"]))
            if source == "ai":
                can_toggle = bool(task.get("task_id")) and not task.get("running")
                check.setEnabled(can_toggle)
                if task.get("running"):
                    check.setToolTip("AI 正在执行，暂不能手动修改状态")
                elif task.get("task_id"):
                    check.setToolTip("手动标记完成；AI 执行成功后仍会自动保存完成记录")
                else:
                    check.setToolTip("这条规划尚未导入统一任务库，无法修改状态")
            else:
                check.setEnabled(bool(task.get("task_id")))
                check.setToolTip("标记完成状态" if task.get("task_id") else "这条旧清单任务尚未导入统一任务库")
            if task.get("task_id") and check.isEnabled():
                check.toggled.connect(lambda checked, identity=task["task_id"]: self.toggle_task(identity, checked))
            row.insertWidget(0, check, 0, Qt.AlignmentFlag.AlignTop)
            layout.addLayout(row)
        layout.addStretch(1)
