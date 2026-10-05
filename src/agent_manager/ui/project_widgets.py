"""Read-only project presentation from existing Markdown; no second source of truth."""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QGridLayout, QHBoxLayout,
    QScrollArea, QTextBrowser, QSizePolicy, QStackedWidget, QStyle, QFrame)

from ..workbench import frontmatter
from ..project_workspaces import STATES
from .components import nav_icon
from .today_widgets import Section, Metric, TodayDelegate, COLORS, ROW_DATA, label, link


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
    """Count only explicit checkboxes or a recognized task table, never linked documents."""
    _, body = frontmatter(text)
    lines = list(outside_fences(body))
    tasks = []
    header = None
    done = {"done", "completed", "complete", "完成", "已完成"}
    for line in lines:
        check = re.match(r"^\s*[-*+] \[([ xX])\]\s+(.+)", line)
        if check:
            tasks.append({"text": check[2].strip(), "done": check[1].casefold() == "x"})
            continue
        if not line.strip().startswith("|"):
            header = None
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if "状态" in cells and any(key in cells for key in ("动作", "任务")):
            header = (cells.index("状态"), cells.index("动作" if "动作" in cells else "任务"))
        elif (header and max(header) < len(cells) and cells[header[1]]
                and not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)):
            tasks.append({"text": cells[header[1]], "done": cells[header[0]].casefold() in done})
    return {"tasks": tasks, "total": len(tasks), "done": sum(t["done"] for t in tasks)}


def overview_snapshot(status, tasks, handoff, summaries):
    metadata, _ = frontmatter(status)
    blocks = sections(status)
    def find(words, default):
        return "\n\n".join(body for name, body in blocks if any(word in name for word in words)) or default
    risks = []
    for line in outside_fences(status):
        if re.match(r"^\s*[-*+]\s+(?:\*\*)?(?:阻塞|风险|待确认|待决策)[：:]", line):
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


class ProjectCardDelegate(TodayDelegate):
    def __init__(self, parent):
        super().__init__(parent, "project")
        self.row_height = 110

    def paint(self, painter, option, index):
        rect = option.rect.adjusted(4, 3, -4, -3)
        data = index.data(ROW_DATA) or {}
        painter.save()
        painter.setClipRect(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(QColor("#a9c6ff" if selected else "#edf1f8"))
        painter.setBrush(QColor("#eef5ff" if selected else "#ffffff"))
        painter.drawRoundedRect(rect, 10, 10)
        color = COLORS[data.get("position", 0) % len(COLORS)]
        tile = QRect(rect.x() + 12, rect.y() + 27, 38, 38)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(tile, 10, 10)
        painter.drawPixmap(tile.x() + 9, tile.y() + 9, nav_icon(3, "#ffffff").pixmap(20, 20))
        x, width = rect.x() + 61, rect.width() - 72
        for dy, text, ink, bold, size in (
            (10, data.get("id", ""), "#8090ac", False, 11),
            (31, data.get("name", ""), "#192c52", True, 15),
            (56, data.get("state_label", ""), color, False, 12),
            (78, "点击继续工作", "#8c9ab0", False, 11)):
            self.text(painter, QRect(x, rect.y() + dy, width, 20), text, ink, bold, size)
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
    """A wrapped action row and long next step must never be compressed by the tabs."""
    def fit(self):
        if self.layout():
            self.setMinimumHeight(self.layout().totalHeightForWidth(self.width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()


class ProjectOverview(QWidget):
    """Card overview and a full source toggle; both use the same loaded documents."""
    def __init__(self, source):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(label("围绕当前阶段继续工作", "TodayMuted"), 1)
        self.toggle = link("查看状态原文", self.toggle_source)
        row.addWidget(self.toggle)
        layout.addLayout(row)
        self.stack = QStackedWidget()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        content = QWidget()
        self.grid = QGridLayout(content)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(10)
        self.cards, self.previews = [], {}
        for key, title, icon in (("goals", "目标与验收", 15), ("progress", "当前进展", 6),
                ("summary", "本阶段总结", 9), ("next", "下一步行动", 14),
                ("risks", "风险 / 待确认", 11), ("recent", "最近记录", 13)):
            card = Section(title, icon)
            view = QTextBrowser()
            view.setObjectName("ProjectPreview")
            view.setOpenExternalLinks(True)
            view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            view.setMinimumWidth(0)
            view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            card.body.addWidget(view)
            card.body.addStretch(1)
            self.cards.append(card)
            self.previews[key] = view
        self.scroll.setWidget(content)
        self.stack.addWidget(self.scroll)
        self.stack.addWidget(source)
        layout.addWidget(self.stack, 1)
        self.columns = 0

    def toggle_source(self):
        self.stack.setCurrentIndex(1 - self.stack.currentIndex())
        self.toggle.setText("回到状态卡片" if self.stack.currentIndex() else "查看状态原文")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        columns = 3 if self.width() >= 840 else 2 if self.width() >= 650 else 1
        if columns != self.columns:
            self.columns = columns
            for i, card in enumerate(self.cards):
                self.grid.addWidget(card, i // columns, i % columns)
            for column in range(3):
                self.grid.setColumnStretch(column, 1 if column < columns else 0)
        self.fit_previews()

    def fit_previews(self):
        width = max(150, self.width() // max(1, self.columns) - 42)
        for view in self.previews.values():
            measured = view.document().clone()
            measured.setTextWidth(width)
            view.setFixedHeight(max(65, min(145, int(measured.size().height()) + 18)))

    def show_snapshot(self, snapshot, base_url):
        for key, view in self.previews.items():
            text = snapshot.get(key, "选择项目查看资料。")
            # Keep the overview bounded. Full canonical records remain in the tabs/source view.
            preview = text[:900] + ("\n\n… 更多内容见对应文档页。" if len(text) > 900 else "")
            view.document().setBaseUrl(base_url)
            view.setMarkdown(preview)
        self.fit_previews()
