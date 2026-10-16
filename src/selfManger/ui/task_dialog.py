"""Shared, date-precise task entry used by the workbench, plan and project pages."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QCheckBox, QTextEdit, QVBoxLayout)

from ..project_workspaces import ProjectWorkspace
from ..domain import UserError


class UnifiedTaskDialog(QDialog):
    def __init__(self, window, *, kind="task", due_date: date | None = None,
                 project_id="", title="", record=None):
        super().__init__(window)
        self.window = window
        self.record = record or {}
        self.setWindowTitle("新建任务" if kind == "task" else "新建日程")
        self.setMinimumSize(560, 530)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(12)
        heading = QLabel("新建任务" if kind == "task" else "新建日程")
        heading.setObjectName("TodayDialogTitle")
        root.addWidget(heading)
        subtitle = QLabel("保存到统一任务库，工作台、计划和关联项目会读取同一条记录。日期可留空，按天安排，不记录小时段。")
        subtitle.setObjectName("TodayMuted")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        form = QFormLayout()
        form.setContentsMargins(16, 14, 16, 16)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.title = QLineEdit(title)
        self.title.setMaxLength(240)
        self.title.setPlaceholderText("例如：完成资源库模块的交互设计")
        self.title.setMinimumHeight(40)
        form.addRow("名称 *", self.title)

        self.kind = QComboBox()
        self.kind.addItem("普通任务", "task")
        self.kind.addItem("日程", "schedule")
        self.kind.setCurrentIndex(0 if kind == "task" else 1)
        self.kind.setMinimumHeight(38)
        form.addRow("类型", self.kind)

        self.has_date = QCheckBox("设置日期")
        self.has_date.setChecked(due_date is not None)
        self.date = QDateEdit(QDate(due_date.year, due_date.month, due_date.day) if due_date else QDate.currentDate())
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.date.setMinimumHeight(38)
        date_row = QHBoxLayout()
        date_row.addWidget(self.has_date)
        date_row.addWidget(self.date, 1)
        form.addRow("日期", date_row)
        self.has_date.toggled.connect(self.date.setEnabled)
        self.date.setEnabled(due_date is not None)

        self.project = QComboBox()
        self.project.setEditable(True)
        self.project.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.project.setMinimumHeight(38)
        self.project.setPlaceholderText("可选择或输入项目编号")
        self._load_projects(project_id)
        form.addRow("关联项目", self.project)

        self.goal = QComboBox()
        self.goal.setEditable(True)
        self.goal.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.goal.setMinimumHeight(38)
        self.goal.setPlaceholderText("可选择或输入目标编号")
        self._load_goals(record.get("goal_id", "") if record else "")
        form.addRow("关联目标", self.goal)

        self.priority = QComboBox()
        for text, value in (("普通", ""), ("高", "high"), ("中", "medium"), ("低", "low")):
            self.priority.addItem(text, value)
        self.priority.setMinimumHeight(38)
        form.addRow("优先级", self.priority)

        self.status = QComboBox()
        for text, value in (("未开始", "planned"), ("进行中", "active"), ("已完成", "done")):
            self.status.addItem(text, value)
        self.status.setMinimumHeight(38)
        form.addRow("状态", self.status)

        self.source = QComboBox()
        self.source.addItem("手动编写", "manual")
        self.source.addItem("AI 规划", "ai")
        self.source.setMinimumHeight(38)
        form.addRow("来源", self.source)

        self.notes = QTextEdit()
        self.notes.setPlaceholderText("补充说明（可选）")
        self.notes.setFixedHeight(92)
        form.addRow("备注", self.notes)
        if record:
            for widget, key in ((self.priority, "priority"), (self.source, "source"), (self.status, "status")):
                index = widget.findData(record.get(key, ""))
                if index >= 0:
                    widget.setCurrentIndex(index)
            self.notes.setPlainText(record.get("notes", ""))
        self.source.currentIndexChanged.connect(self._sync_ai_edit_policy)
        self._sync_ai_edit_policy()
        card = QFrame()
        card.setObjectName("TodayDialogCard")
        card.setLayout(form)
        root.addWidget(card, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Save).setProperty("primary", True)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(bool(title.strip()))
        self.title.textChanged.connect(lambda value: buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(bool(value.strip())))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _sync_ai_edit_policy(self, *_):
        ai_source = self.source.currentData() == "ai"
        existing_ai = bool(self.record and self.record.get("source") == "ai")
        if ai_source and not existing_ai and self.status.currentData() == "done":
            self.status.setCurrentIndex(max(0, self.status.findData("planned")))
        self.source.setEnabled(not existing_ai)
        self.status.setEnabled(not ai_source)
        if ai_source:
            self.status.setToolTip("AI 规划任务必须由 Agent 执行并验证后才能完成。")
        else:
            self.status.setToolTip("")

    def _load_projects(self, selected):
        self.project.addItem("", "")
        root = self.window.store.setting("project_workspace", "")
        if root:
            try:
                entries = ProjectWorkspace(Path(root)).registry().get("projects", [])
                for item in entries:
                    self.project.addItem(f'{item["id"]} · {item["name"]}', item["id"])
            except (OSError, ValueError, KeyError, UserError):
                pass
        if selected:
            match = next((i for i in range(self.project.count()) if self.project.itemData(i) == selected), None)
            if match is None:
                self.project.setCurrentText(selected)
            else:
                self.project.setCurrentIndex(match)

    def _load_goals(self, selected):
        self.goal.addItem("", "")
        try:
            from ..planbook import PlanBook
            from ..workbench import work_root
            goals = PlanBook(work_root(self.window.store)).scan("goal")["items"]
            for item in goals:
                self.goal.addItem(f'{item["id"]} · {item["title"]}', item["id"])
        except (OSError, ValueError, KeyError, UserError):
            pass
        if selected:
            match = next((i for i in range(self.goal.count()) if self.goal.itemData(i) == selected), None)
            if match is None:
                self.goal.setCurrentText(selected)
            else:
                self.goal.setCurrentIndex(match)

    def values(self):
        project_id = self.project.currentData()
        if project_id is None:
            project_id = self.project.currentText().split(" · ", 1)[0].strip()
        goal_id = self.goal.currentData()
        if goal_id is None:
            goal_id = self.goal.currentText().split(" · ", 1)[0].strip()
        project_root = self.window.store.setting("project_workspace", "")
        if self.record and project_id == self.record.get("project_id"):
            project_root = self.record.get("project_root") or project_root
        project_root = str(Path(project_root).expanduser().resolve()) if project_root and project_id else None
        return {
            "title": self.title.text().strip(),
            "kind": self.kind.currentData(),
            "due_date": self.date.date().toPython().isoformat() if self.has_date.isChecked() else None,
            "project_id": project_id,
            "project_root": project_root,
            "goal_id": goal_id,
            "priority": self.priority.currentData(),
            "status": self.status.currentData(),
            "source": self.source.currentData(),
            "notes": self.notes.toPlainText(),
        }
