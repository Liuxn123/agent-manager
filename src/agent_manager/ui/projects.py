from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, QEvent, QFileSystemWatcher
from PySide6.QtGui import QDesktopServices, QTextOption
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QLabel, QPushButton,
    QComboBox, QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSplitter, QTextBrowser, QDialog, QDialogButtonBox, QFormLayout, QTextEdit, QCheckBox,
    QFileDialog, QMessageBox, QListWidget, QListWidgetItem, QInputDialog, QToolButton, QMenu,
    QFrame, QSizePolicy, QScrollArea, QGridLayout)

from ..domain import Resource, UserError
from ..project_workspaces import ProjectWorkspace, STATES
from ..obsidian import project_uri, index_uri
from ..workbench import (PHASES, TYPES, Catalog, work_root, project_context, set_project_details,
    stage_template, stage_prompt, append_stage, summaries)
from ..storage import now
from .components import FlowLayout, nav_icon
from .today_widgets import COLORS, ROW_DATA, label
from .project_widgets import (ProjectCardDelegate, ProjectMetric, ProjectMetrics, ProjectHero,
    ProjectOverview, overview_snapshot)


class ProjectLogView(QWidget):
    """Present dated entries while keeping HANDOFF as the single source."""
    def __init__(self, view):
        super().__init__()
        self.view, self.text, self.entries = view, "", []
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.selector = QComboBox()
        self.selector.currentIndexChanged.connect(self.render)
        row.addWidget(self.selector, 1)
        self.all = QCheckBox("查看完整交接原文")
        self.all.toggled.connect(self.render)
        row.addWidget(self.all)
        layout.addLayout(row)
        layout.addWidget(view, 1)

    def set_text(self, text):
        self.text = text
        headings = list(re.finditer(r"(?m)^## (.+)$", text))
        self.entries = [(match.group(1), text[match.start():headings[i + 1].start() if i + 1 < len(headings) else len(text)].strip())
                        for i, match in enumerate(headings) if re.search(r"\d{4}-\d{2}-\d{2}", match.group(1))]
        self.entries.reverse()
        self.selector.blockSignals(True)
        self.selector.clear()
        for title, _ in self.entries:
            self.selector.addItem(title)
        self.selector.blockSignals(False)
        self.render()

    def render(self, *_):
        row = self.selector.currentIndex()
        if self.all.isChecked():
            self.view.setMarkdown(self.text)
        elif 0 <= row < len(self.entries):
            self.view.setMarkdown(self.entries[row][1])
        else:
            self.view.setPlainText("还没有按日期记录的日志。点击“写日志”记录完成内容、验证和下一步。原有交接内容可勾选“查看完整交接原文”。")


class ProjectDialog(QDialog):
    def __init__(self, parent, legacy=None):
        super().__init__(parent)
        self.setWindowTitle("新建项目 · 按既有规范初始化")
        self.resize(580, 370)
        layout = QVBoxLayout(self)
        note = QLabel("填写名称和目标即可。自动分配编号，创建状态、任务和日志交接文件；业务目录按需添加。")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.name = QLineEdit()
        self.goal = QTextEdit()
        self.goal.setPlaceholderText("这个项目希望完成什么？")
        self.entry = QCheckBox("在工作区的 Obsidian 建立同一份资料入口")
        self.entry.setChecked(True)
        if legacy:
            self.setWindowTitle("从旧归档继续 · 新建编号项目")
            note.setText("原归档保留原位。为接下来的工作分配新编号，创建状态、任务和日志，并引用原资料；不会搬动或复制业务文件。")
            self.name.setText(legacy["name"] + "-接续")
            self.goal.setPlainText("接续旧项目 " + legacy["name"] + "（" + legacy["id"] + "）。\n原归档资料：" + legacy["directory"] + "\n下一步：核对原项目状态、业务代码位置和本次目标。")
        form.addRow("项目名称", self.name)
        form.addRow("项目目标", self.goal)
        form.addRow("", self.entry)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("一键初始化")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.confirm)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def confirm(self):
        if not self.name.text().strip() or not self.goal.toPlainText().strip():
            QMessageBox.information(self, "补充项目目标", "请填写项目名称和目标，其他内容以后再补。")
            return
        self.accept()


class ProjectPage(QWidget):
    def __init__(self, window, backup_page):
        super().__init__()
        self.window = window
        self.rows = []
        self.document_version = 0
        self.note_version = 0
        self.watch_paths = []
        self.move_task = ""
        self.move_watchers = []
        self.move_selection = None
        window.task_completed.connect(self.move_finished)
        self.preferred_note = ""
        self.preferred_project = ""
        self.status_text, self.task_text, self.project_catalog = "", "", []
        self.document_errors = {}
        self.project_details = {}
        self.closed = False
        self.setObjectName("ProjectPage")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        title_group = QVBoxLayout()
        title_group.setSpacing(4)
        title_group.addWidget(label("项目", "Title"))
        title_group.addWidget(label("看清当前阶段，找到下一步，继续工作。", "Subtitle"))
        heading.addLayout(title_group, 1)
        self.new_button = QPushButton("新建项目")
        self.new_button.setProperty("primary", True)
        self.new_button.setIcon(nav_icon(9))
        self.new_button.clicked.connect(self.create_project)
        self.obsidian_button = QPushButton("Obsidian 中打开")
        self.obsidian_button.setToolTip("打开当前标签对应的原文件；笔记页打开所选笔记")
        self.obsidian_button.clicked.connect(self.open_in_obsidian)
        heading.addWidget(self.new_button)
        heading.addWidget(self.obsidian_button)
        layout.addLayout(heading)
        toolbar = QHBoxLayout()
        self.choose_button = QPushButton("选择工作区…")
        self.choose_button.setIcon(nav_icon(3))
        self.choose_button.clicked.connect(self.choose_workspace)
        toolbar.addWidget(self.choose_button)
        self.root_label = QLabel(self)
        self.root_label.hide()
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索项目名称或编号…")
        self.search.textChanged.connect(self.render)
        toolbar.addWidget(self.search, 1)
        self.filter = QComboBox()
        for title, value in (("未归档", "current"), ("已归档", "archived"), ("全部", "all")):
            self.filter.addItem(title, value)
        self.filter.currentIndexChanged.connect(self.render)
        toolbar.addWidget(self.filter)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        toolbar.addWidget(refresh)
        layout.addLayout(toolbar)

        # Compatibility controls keep their slots/state; their actions live in one menu.
        self.open_button = QPushButton("打开文件夹", self)
        self.open_button.clicked.connect(self.open_project)
        self.edit_button = QPushButton("编辑当前文档", self)
        self.edit_button.clicked.connect(self.edit_document)
        self.move_button = QPushButton("归档项目…", self)
        self.move_button.clicked.connect(self.move_project)
        self.stage_button = QPushButton("阶段与关联资料", self)
        self.stage_button.clicked.connect(self.edit_stage)
        self.obsidian_menu = QToolButton(self)
        self.obsidian_menu.hide()
        self.myself_status = QLabel(self)
        self.myself_status.hide()
        self.more_button = QPushButton("更多操作")
        self.more_menu = QMenu(self.more_button)
        self.more_actions = {}
        for control in (self.stage_button, self.edit_button, self.open_button, self.move_button):
            control.hide()
            action = self.more_menu.addAction(control.text(), control.click)
            self.more_actions[control] = action
        self.more_menu.addSeparator()
        self.more_menu.addAction("Obsidian 项目总览", lambda: self.open_overview(False))
        self.more_menu.addAction("Obsidian 归档索引", lambda: self.open_overview(True))
        self.more_menu.addAction("刷新 Obsidian 索引", self.refresh_indexes)
        self.more_menu.aboutToShow.connect(self.update_selection_actions)
        self.more_button.setMenu(self.more_menu)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.project_list = QFrame()
        self.project_list.setObjectName("TodayCard")
        left = QVBoxLayout(self.project_list)
        left.setContentsMargins(10, 14, 10, 10)
        self.list_title = label("项目列表", "TodaySectionTitle")
        left.addWidget(self.list_title)
        self.table = QTableWidget(0, 3)
        self.table.setObjectName("ProjectCards")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().hide()
        self.table.setColumnHidden(1, True)
        self.table.setColumnHidden(2, True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setShowGrid(False)
        self.table.setItemDelegateForColumn(0, ProjectCardDelegate(self.table))
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.verticalHeader().setDefaultSectionSize(110)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        left.addWidget(self.table, 1)
        self.project_list.setMinimumWidth(205)
        self.project_list.setMaximumWidth(330)
        split.addWidget(self.project_list)
        detail = QWidget()
        detail.setObjectName("ProjectDetail")
        detail.setMinimumWidth(0)
        details = QVBoxLayout(detail)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(9)
        hero = ProjectHero()
        self.hero = hero
        hero.setObjectName("TodayCard")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(16, 12, 16, 12)
        hero_heading = QHBoxLayout()
        self.project_icon = QLabel()
        self.project_icon.setObjectName("ProjectIcon")
        self.project_icon.setPixmap(nav_icon(3, "#ffffff").pixmap(26, 26))
        self.project_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.project_icon.setFixedSize(48, 48)
        hero_heading.addWidget(self.project_icon)
        hero_text = QVBoxLayout()
        hero_text.setSpacing(3)
        self.project_name = label("选择一个项目", "ProjectName")
        self.project_name.setWordWrap(True)
        self.project_name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.project_identity = label("项目的状态、任务、记录都在这里", "TodayMuted")
        self.project_identity.setWordWrap(True)
        hero_text.addWidget(self.project_name)
        hero_text.addWidget(self.project_identity)
        hero_heading.addLayout(hero_text, 1)
        hero_layout.addLayout(hero_heading)
        self.stage_label = label("选择项目后显示当前阶段和下一步。", "ProjectNext")
        self.stage_label.setWordWrap(True)
        self.stage_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        hero_layout.addWidget(self.stage_label)
        self.project_meta = label("", "TodayMuted")
        self.project_meta.setWordWrap(True)
        hero_layout.addWidget(self.project_meta)
        hero_actions = FlowLayout()
        self.summary_button = QPushButton("写阶段总结")
        self.summary_button.setIcon(nav_icon(9))
        self.summary_button.clicked.connect(self.write_stage_summary)
        self.log_button = QPushButton("写日志")
        self.log_button.setIcon(nav_icon(13))
        self.log_button.clicked.connect(self.append_log)
        for control in (self.summary_button, self._resource_button(), self.log_button, self.more_button):
            hero_actions.addWidget(control)
        hero_layout.addLayout(hero_actions)
        details.addWidget(hero)
        self.metrics = [ProjectMetric(title, color, icon) for title, color, icon in (
            ("当前阶段", COLORS[0], 15), ("清单任务", COLORS[2], 9),
            ("项目日志", COLORS[3], 13), ("关联资料", COLORS[1], 6))]
        self.metrics_row = ProjectMetrics(self.metrics)
        details.addWidget(self.metrics_row)
        self.documents = QTabWidget()
        self.documents.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.documents.tabBar().setUsesScrollButtons(True)
        self.views = []
        self.document_paths = ["agent/STATUS.md", "agent/TASKS.md", "agent/HANDOFF.md"]
        for name in ("当前状态", "任务入口", "日志与交接"):
            view = QTextBrowser()
            view.setOpenExternalLinks(True)
            view.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
            text_option = QTextOption()
            text_option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            view.document().setDefaultTextOption(text_option)
            if name == "日志与交接":
                self.log_view = ProjectLogView(view)
                self.documents.addTab(self.log_view, "日志")
            elif name == "当前状态":
                self.overview = ProjectOverview(view)
                self.documents.addTab(self.overview, name)
            else:
                self.documents.addTab(view, "任务" if name == "任务入口" else name)
            self.views.append(view)
        notes_page = QWidget()
        notes_layout = QVBoxLayout(notes_page)
        notes_layout.setContentsMargins(8, 8, 8, 8)
        note_toolbar = QHBoxLayout()
        note_hint = QLabel("笔记保存在真实项目内，在 Obsidian 编辑同一份正文。")
        note_hint.setWordWrap(True)
        note_toolbar.addWidget(note_hint, 1)
        self.new_note_button = QPushButton("新建笔记")
        self.new_note_button.clicked.connect(self.create_note)
        note_toolbar.addWidget(self.new_note_button)
        notes_layout.addLayout(note_toolbar)
        self.note_list = QListWidget()
        self.note_list.setMaximumHeight(150)
        self.note_list.currentItemChanged.connect(self.note_changed)
        notes_layout.addWidget(self.note_list)
        self.note_view = QTextBrowser()
        self.note_view.setOpenExternalLinks(True)
        notes_layout.addWidget(self.note_view, 1)
        self.documents.addTab(notes_page, "笔记")
        self.summary_view = QTextBrowser()
        self.summary_view.setOpenExternalLinks(True)
        self.documents.addTab(self.summary_view, "阶段总结")
        related_page = QWidget()
        related_layout = QVBoxLayout(related_page)
        related_hint = QLabel("双击打开关联 Agent 或资源。资料与 Obsidian 共用。")
        related_hint.setWordWrap(True)
        related_layout.addWidget(related_hint)
        self.related_list = QListWidget()
        self.related_list.setWordWrap(True)
        self.related_list.itemDoubleClicked.connect(self.open_related)
        related_layout.addWidget(self.related_list)
        self.documents.addTab(related_page, "资源")
        self.documents.setMinimumWidth(0)
        self.documents.setMinimumHeight(400)
        self.documents.currentChanged.connect(self.update_selection_actions)
        details.addWidget(self.documents, 1)
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setWidget(detail)
        detail_scroll.setMinimumWidth(0)
        split.addWidget(detail_scroll)
        split.setChildrenCollapsible(False)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 880])
        layout.addWidget(split, 1)
        self.message = QLabel("请选择已有工作区，或选择一个空文件夹初始化。")
        self.message.setObjectName("Subtitle")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.backup_page = backup_page
        backup_page.setParent(self)
        backup_page.hide()
        self.refresh_myself()
        self.project_rows = []
        self.watcher = QFileSystemWatcher(self)
        self.reload_timer = QTimer(self)
        self.reload_timer.setSingleShot(True)
        self.reload_timer.setInterval(500)
        self.reload_timer.timeout.connect(self.reload_external_changes)
        self.watcher.fileChanged.connect(lambda _: self.reload_timer.start())
        self.watcher.directoryChanged.connect(lambda _: self.reload_timer.start())
        self.window.installEventFilter(self)
        self.render()
        self.new_button.setEnabled(bool(self.root()))
        if not self.root():
            self.refresh()
        # Load project files when this page is opened, rather than at startup.

    def _resource_button(self):
        control = QPushButton("查看项目资源")
        control.clicked.connect(self.show_project_resources)
        self.resource_button = control
        return control

    def render_related(self, item):
        self.related_list.clear()
        agents = self.project_details.get("agents", [])
        linked = self.project_details.get("resources", [])
        known = set()
        for resource in self.window.store.resources():
            if resource.id in agents or item["id"] in resource.options.get("project_ids", []):
                cell = QListWidgetItem("Agent · " + resource.name)
                cell.setData(Qt.ItemDataRole.UserRole, ("agent", resource.id))
                self.related_list.addItem(cell)
                known.add(resource.id)
        for resource in self.project_catalog:
            if resource["id"] in linked or item["id"] in resource["metadata"].get("projects", []):
                cell = QListWidgetItem(TYPES[resource["type"]] + " · " + resource["name"])
                cell.setData(Qt.ItemDataRole.UserRole, ("resource", resource["id"]))
                self.related_list.addItem(cell)
                known.add(resource["id"])
        for identity in [*agents, *linked]:
            if identity not in known:
                self.related_list.addItem("暂未找到关联资料：" + identity + "（保留引用）")
        if not self.related_list.count():
            self.related_list.addItem("还没有关联资料。点“阶段与关联资料”选择，或在资源库填写适用项目编号。")

    def open_related(self, item):
        value = item.data(Qt.ItemDataRole.UserRole)
        if not value:
            return
        kind, identity = value
        if kind == "agent":
            self.window.navigation.setCurrentRow(self.window.AGENT)
            self.window.agent_page.refresh(identity)
        else:
            self.show_project_resources()
            self.window.catalog_page.search.setText(next((i["name"] for i in self.project_catalog if i["id"] == identity), ""))

    def show_project_resources(self):
        item = self.selected()
        if item:
            page = self.window.catalog_page
            page.set_scope(project=item["id"], related=self.project_details.get("resources", []))
            self.window.navigation.setCurrentRow(self.window.LIBRARY)

    def edit_stage(self):
        item = self.selected()
        if not item or not self.status_text:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("项目阶段与关联资料")
        dialog.resize(680, 590)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("只补充 STATUS 字段；不改变任务、交接正文和项目编号。阶段可自行命名。"))
        phase = QComboBox()
        phase.setEditable(True)
        phase.addItems(PHASES)
        phase.setCurrentText(self.project_details["phase"])
        layout.addWidget(phase)
        picks = []
        groups = [("关联 Agent", "agents", [(r.id, r.name) for r in self.window.store.resources() if r.kind in {"agent", "hermes_local", "hermes_server"}]),
                  ("关联 Prompt / Skill / 资源", "resources", [(r["id"], r["name"]) for r in self.project_catalog])]
        for title, key, rows in groups:
            layout.addWidget(QLabel(title))
            listing = QListWidget()
            existing = self.project_details.get(key, [])
            for identity, name in rows:
                cell = QListWidgetItem(name)
                cell.setData(Qt.ItemDataRole.UserRole, identity)
                cell.setFlags(cell.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                cell.setCheckState(Qt.CheckState.Checked if identity in existing else Qt.CheckState.Unchecked)
                listing.addItem(cell)
            layout.addWidget(listing, 1)
            picks.append((listing, [v for v in existing if v not in {r[0] for r in rows}]))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        expected = self.status_text
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = [[*unknown, *[listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(listing.count()) if listing.item(i).checkState() == Qt.CheckState.Checked]] for listing, unknown in picks]
        workspace, name = self.workspace(), phase.currentText()
        self.submit("更新项目阶段与关联", lambda context: set_project_details(workspace, item["id"], name, values[0], values[1], expected, context), lambda _: self.selection_changed(), persist_result=False)

    def write_stage_summary(self):
        item = self.selected()
        if not item or not self.status_text:
            return
        phase = self.project_details["phase"]
        prompt = stage_prompt(item["name"], phase, self.status_text, self.task_text)
        dialog = QDialog(self)
        dialog.setWindowTitle("阶段总结 · " + phase)
        dialog.resize(780, 650)
        layout = QVBoxLayout(dialog)
        hint = QLabel("人工填写，或复制 Prompt 到常用 Agent，再把结果粘贴回来。保存后追加到 HANDOFF，原交接保留。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        from PySide6.QtWidgets import QApplication
        copy = QPushButton("复制生成总结 Prompt")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(prompt))
        layout.addWidget(copy)
        editor = QTextEdit()
        editor.setAcceptRichText(False)
        editor.setPlainText(stage_template(phase))
        layout.addWidget(editor, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("追加阶段总结")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        workspace, body = self.workspace(), editor.toPlainText()
        def saved(_):
            self.selection_changed()
            self.documents.setCurrentIndex(4)
        self.submit("追加项目阶段总结", lambda context: append_stage(workspace, item["id"], phase, body, context), saved, persist_result=False)

    def myself_resource(self):
        return next((r for r in self.window.store.resources() if r.kind in {"project", "vault"} and Path(r.options.get("path", "")).name.casefold() == "myself"), None)

    def refresh_myself(self):
        from ..maintenance import backup_health
        resource = self.myself_resource()
        self.myself_status.setText("myself：" + (backup_health(self.window.store, resource, self.window.service.backup_root())["state"] if resource else "未登记"))

    def myself_action(self, action):
        resource = self.myself_resource()
        if not resource:
            self.window.add_resource("vault")
        elif action == "edit":
            self.window.edit_resource(resource)
        else:
            self.window.perform(resource, action)

    def show_backup_resource(self, resource):
        self.window.open_safety_resource(resource)

    def root(self) -> Path | None:
        value = self.window.store.setting("project_workspace", "")
        return Path(value) if value else None

    def workspace(self) -> ProjectWorkspace:
        root = self.root()
        if not root:
            raise UserError("请先选择项目工作区。")
        return ProjectWorkspace(root)

    def owner(self) -> Resource | None:
        root = self.root()
        return Resource("项目工作区", "project", {"path": str(root)}, hashlib.sha256(str(root).encode()).hexdigest()) if root else None

    def submit(self, title, operation, callback=None, persist_result=True):
        owner = self.owner()
        if owner is None:
            QMessageBox.information(self, "选择工作区", "先选择保存 projects、archive 和 agent 的工作区根目录。")
            return
        def locked(context):
            with self.window.service.locks.acquire(self.window.service.lock_keys(owner)):
                return operation(context)
        return self.window.submit(owner, title, locked, callback, persist_result=persist_result)

    def choose_workspace(self):
        if self.owner() and self.window.is_busy(self.owner().id):
            QMessageBox.information(self, "任务运行中", "请等待本工作区任务结束后切换。")
            return
        selected = QFileDialog.getExistingDirectory(self, "选择项目工作区根目录", str(self.root() or Path.home()))
        if not selected:
            return
        root = Path(selected)
        try:
            workspace = ProjectWorkspace(root)
            if not workspace.registry_path.exists():
                if any(root.iterdir()):
                    raise UserError("这不是规范工作区，也不是空目录。请选择含 agent/projects.json 的根目录，或新建空文件夹。")
                if QMessageBox.question(self, "初始化工作区", "在这个空目录建立 projects、archive、agent 和 Obsidian 项目索引？") != QMessageBox.StandardButton.Yes:
                    return
                self.window.store.set_setting("project_workspace", str(root))
                self.submit("初始化项目工作区", workspace.initialize, lambda report: self.refresh())
            else:
                workspace.registry()
                self.window.store.set_setting("project_workspace", str(root))
                self.refresh()
        except (OSError, UserError) as exc:
            QMessageBox.warning(self, "无法使用工作区", str(exc))

    def refresh(self):
        if self.closed or self.move_watchers:
            return
        root = self.root()
        self.root_label.setText("工作区：" + str(root) if root else "未选择项目工作区")
        self.choose_button.setText("工作区 · " + (root.name[:18] if root else "选择…"))
        self.choose_button.setToolTip(str(root) if root else "选择规范工作区或初始化空目录")
        self.new_button.setEnabled(bool(root))
        self.obsidian_menu.setEnabled(bool(root))
        if not root:
            self.project_rows = []
            self.render()
            self.project_name.setText("先选择项目工作区")
            self.stage_label.setText("点击上方“工作区 · 选择…”加载已有项目，或初始化一个空文件夹。")
            self.message.setText("当前数据位置：" + str(self.window.store.root) + " · 尚未选择工作区，原有项目文件不会被删除。")
            return
        if self.owner() and self.window.is_busy(self.owner().id):
            self.reload_timer.start()
            return
        def loaded(report):
            self.project_rows = report["projects"]
            self.render()
            self.message.setText(f"{len(self.project_rows)} 个项目 · Obsidian 与管家使用同一份文件；外部修改自动刷新。归档不等于备份。")
        self.submit("读取项目登记与状态", lambda context: self.workspace().list_projects(context), loaded, persist_result=False)

    def selected(self):
        index = self.table.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def render(self):
        selected = self.selected()
        query = self.search.text().strip().casefold()
        mode = self.filter.currentData()
        self.rows = [p for p in self.project_rows if query in (p["id"] + " " + p["name"]).casefold() and (mode == "all" or p["path"].startswith("archive/") == (mode == "archived"))]
        self.list_title.setText(f"项目列表 · {len(self.rows)}")
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        for row, item in enumerate(self.rows):
            for column, value in enumerate((item["id"], item["name"], STATES.get(item["state"], "请检查状态"))):
                cell = QTableWidgetItem(value)
                cell.setToolTip(item["name"] + "\n" + item["id"] + " · " + STATES.get(item["state"], "请检查状态") + "\n" + (item.get("error") or item["directory"]))
                cell.setData(ROW_DATA, {**item, "position": row, "state_label": STATES.get(item["state"], "请检查状态")})
                self.table.setItem(row, column, cell)
        if self.rows:
            identity = self.preferred_project or (selected["id"] if selected else "")
            self.table.selectRow(next((i for i, p in enumerate(self.rows) if p["id"] == identity), 0))
            self.preferred_project = ""
        self.table.blockSignals(False)
        self.selection_changed()

    def update_selection_actions(self, *_):
        item = self.selected()
        enabled = bool(item) and not self.move_watchers
        archived = bool(item and item["path"].startswith("archive/"))
        self.open_button.setEnabled(enabled)
        self.resource_button.setEnabled(enabled)
        self.obsidian_button.setEnabled(enabled and (self.documents.currentIndex() != 3 or self.note_list.currentItem() is not None))
        self.new_note_button.setEnabled(enabled and not archived)
        self.log_button.setEnabled(enabled and not archived and not self.document_errors.get(2))
        self.edit_button.setEnabled(enabled and not archived and self.documents.currentIndex() < 2 and not self.document_errors.get(self.documents.currentIndex()))
        self.move_button.setEnabled(enabled)
        self.move_button.setText("从旧归档继续…" if item and item.get("legacy") else "重新启用…" if archived else "归档项目…")
        self.stage_button.setEnabled(enabled and not archived and bool(self.status_text) and not self.document_errors.get(0))
        self.summary_button.setEnabled(enabled and not archived and bool(self.status_text) and not self.document_errors.get(2))
        for control, action in self.more_actions.items():
            action.setEnabled(control.isEnabled())
            action.setText(control.text())
        self.more_button.setEnabled(enabled)

    def selection_changed(self):
        if self.move_watchers:
            return
        self.status_text, self.task_text = "", ""
        self.document_errors = {}
        self.project_details = {}
        self.update_selection_actions()
        item = self.selected()
        self.document_version += 1
        version = self.document_version
        self.note_version += 1
        previous = self.note_list.currentItem()
        selected_note = self.preferred_note or (previous.data(Qt.ItemDataRole.UserRole) if previous else "")
        self.preferred_note = ""
        self.note_list.blockSignals(True)
        self.note_list.clear()
        self.note_list.blockSignals(False)
        self.note_view.setPlainText("选择项目查看笔记。" if not item else "读取中…")
        for view in self.views:
            view.setPlainText("选择项目查看管理资料。" if not item else "读取中…")
        self.project_name.setText(item["name"] if item else "选择一个项目")
        self.project_identity.setText(item["id"] + " · " + STATES.get(item["state"], "请检查状态") if item else "从左侧选择，或新建项目")
        self.project_meta.clear()
        self.stage_label.setText("正在读取阶段与下一步…" if item else "选择项目后显示当前阶段和下一步。")
        for metric in self.metrics:
            metric.value.setText("—")
            metric.note.setText("读取中…" if item else "请选择项目")
        self.overview.show_snapshot({}, QUrl())
        self.hero.fit()
        if not item:
            self.status_text, self.task_text = "", ""
            self.stage_label.setText("选择项目后显示当前阶段和下一步。")
            self.summary_view.clear()
            self.related_list.clear()
            self.watch_paths = []
            self.update_watches([])
            return
        root, identity = self.root(), item["id"]
        notes_root = work_root(self.window.store)
        def read(context):
            workspace = ProjectWorkspace(root)
            documents, errors = [], []
            for relative in self.document_paths:
                context.checkpoint()
                try:
                    documents.append(workspace.document(identity, relative))
                except (UserError, OSError) as exc:
                    documents.append({"text": "", "path": str(root / item["path"] / relative), "error": str(exc)})
                    errors.append(relative + "：" + str(exc))
            try:
                notes = workspace.notes(identity, context)["notes"]
            except (UserError, OSError) as exc:
                notes = []
                errors.append("笔记：" + str(exc))
            try:
                library = Catalog(notes_root).scan(context)
                errors.extend(library["errors"])
            except (UserError, OSError) as exc:
                library = {"items": []}
                errors.append("资源库：" + str(exc))
            paths = [str(workspace.registry_path), str(workspace.registry_path.parent), *[d["path"] for d in documents], str(workspace.path(item["path"] + "/agent")), str(workspace.path(item["path"])), str(workspace.path(item["path"] + "/笔记"))]
            paths += [str(notes_root / "资源"), *[i["path"] for i in library["items"]]]
            return {"documents": documents, "notes": notes, "watch_paths": paths, "catalog": library["items"], "errors": errors}
        def show(report):
            if self.closed or version != self.document_version:
                return
            self.document_errors = {i: d["error"] for i, d in enumerate(report["documents"]) if d.get("error")}
            self.status_text, self.task_text = report["documents"][0]["text"], report["documents"][1]["text"]
            try:
                self.project_details = project_context(self.status_text, item["state"])
            except UserError as exc:
                self.document_errors[0] = str(exc)
                report["errors"].append("STATUS：" + str(exc))
                self.status_text = ""
                self.project_details = project_context("", item["state"])
            self.project_catalog = report["catalog"]
            self.stage_label.setText(item["name"] + " · " + self.project_details["phase"] + "\n下一步：" + self.project_details["next_step"])
            blocks = summaries(report["documents"][2]["text"])
            self.summary_view.setMarkdown("\n\n---\n\n".join(reversed(blocks)) if blocks else "还没有阶段总结。点击“写阶段总结”，可人工填写或复制 Prompt 到常用 Agent。总结追加到原 HANDOFF 文件。")
            self.render_related(item)
            snapshot = overview_snapshot(self.status_text, self.task_text, report["documents"][2]["text"], blocks)
            phase = self.project_details["phase"]
            self.project_identity.setText(item["id"] + " · " + STATES.get(item["state"], "请检查状态") + " · " + phase)
            self.stage_label.setText("当前阶段：" + phase + "\n下一步：" + self.project_details["next_step"])
            self.project_meta.setText("最近更新：" + snapshot["updated"] + " · Obsidian 与管家共用原文件")
            if report["errors"]:
                self.project_meta.setText("部分资料需要检查：" + "；".join(report["errors"]))
            base_url = QUrl.fromLocalFile(str(Path(report["documents"][0]["path"]).parent) + os.sep)
            self.overview.show_snapshot(snapshot, base_url)
            linked = [self.related_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.related_list.count())]
            agents = sum(bool(value and value[0] == "agent") for value in linked)
            resources = sum(bool(value and value[0] == "resource") for value in linked)
            checklist = snapshot["checklist"]
            values = (phase, f'{checklist["done"]} / {checklist["total"]}' if checklist["total"] else "任务入口",
                f'{snapshot["logs"]} 条', f'{agents + resources} 项')
            notes = ("可自定义阶段", "清单完成 / 总数" if checklist["total"] else "查看任务原记录",
                "日期日志，保留原文", f"{agents} Agent · {resources} 资源")
            for metric, value, note in zip(self.metrics, values, notes):
                metric.value.setText(value[:7] + "…" if metric is self.metrics[0] and len(value) > 7 else value)
                metric.value.setToolTip(value)
                metric.note.setText(note)
            self.hero.fit()
            self.update_selection_actions()
            for index, (view, document) in enumerate(zip(self.views, report["documents"])):
                view.document().setBaseUrl(QUrl.fromLocalFile(str(Path(document["path"]).parent) + os.sep))
                if index in self.document_errors:
                    view.setPlainText("这份文档暂时无法读取：" + self.document_errors[index] + "\n请打开项目文件夹检查；其他资料仍可查看。原文件未改动。")
                elif view is self.views[2]:
                    self.log_view.set_text(document["text"])
                else:
                    view.setMarkdown(document["text"])
            self.note_list.blockSignals(True)
            for note in report["notes"]:
                cell = QListWidgetItem(note["name"])
                cell.setData(Qt.ItemDataRole.UserRole, note["relative"])
                self.note_list.addItem(cell)
            if self.note_list.count():
                row = next((i for i in range(self.note_list.count()) if self.note_list.item(i).data(Qt.ItemDataRole.UserRole) == selected_note), 0)
                self.note_list.setCurrentRow(row)
            self.note_list.blockSignals(False)
            self.watch_paths = report["watch_paths"]
            self.note_changed()
        # A dedicated read does not compete with the selected lifecycle task's resource identity.
        self.window.submit(None, "读取项目管理文档", read, show, persist_result=False)

    def update_watches(self, paths):
        if self.watcher.property("projectMovePaused"):
            return
        existing = self.watcher.files() + self.watcher.directories()
        if existing:
            self.watcher.removePaths(existing)
        available = list(dict.fromkeys(str(p) for p in paths if Path(p).exists()))
        if available:
            self.watcher.addPaths(available)

    def eventFilter(self, watched, event):
        if not self.closed and watched is self.window and event.type() == QEvent.Type.WindowActivate and self.isVisible():
            self.reload_timer.start()
        return super().eventFilter(watched, event)

    def reload_external_changes(self):
        if self.closed or not self.isVisible() or not self.root():
            return
        if self.owner() and self.window.is_busy(self.owner().id):
            self.reload_timer.start()
            return
        self.refresh()

    def stop_updates(self):
        self.closed = True
        self.reload_timer.stop()
        self.update_watches([])
        self.window.removeEventFilter(self)

    def note_changed(self, *_):
        item, note = self.selected(), self.note_list.currentItem()
        self.obsidian_button.setEnabled(bool(item) and (self.documents.currentIndex() != 3 or bool(note)))
        self.note_version += 1
        version = self.note_version
        if not item or not note:
            self.note_view.setPlainText("还没有项目笔记。点“新建笔记”，然后在 Obsidian 编辑正文。" if item else "选择项目查看笔记。")
            self.update_watches(self.watch_paths if item else [])
            return
        workspace, identity, relative = self.workspace(), item["id"], note.data(Qt.ItemDataRole.UserRole)
        def loaded(report):
            if version != self.note_version:
                return
            self.note_view.document().setBaseUrl(QUrl.fromLocalFile(str(Path(report["path"]).parent) + os.sep))
            self.note_view.setMarkdown(report["text"])
            self.update_watches([*self.watch_paths, report["path"]])
        self.window.submit(None, "读取所选项目笔记", lambda context: workspace.note_document(identity, relative), loaded, persist_result=False)

    def create_note(self):
        item = self.selected()
        if not item or item["path"].startswith("archive/"):
            return
        title, accepted = QInputDialog.getText(self, "新建项目笔记", "笔记名称（保存到项目的“笔记”目录）：")
        if not accepted:
            return
        workspace = self.workspace()
        def created(report):
            self.preferred_note = report["relative"]
            self.selection_changed()
        self.submit("创建项目笔记", lambda context: workspace.create_note(item["id"], title, context), created)

    def launch_obsidian(self, report):
        if not QDesktopServices.openUrl(QUrl(report["uri"])):
            QMessageBox.warning(self, "未能打开 Obsidian", "请安装并运行 Obsidian 一次以注册 obsidian:// 接口。原文件仍在：\n" + report["path"])

    def open_in_obsidian(self):
        item = self.selected()
        index = self.documents.currentIndex()
        note = self.note_list.currentItem()
        if not item or index == 3 and not note:
            return
        relative = self.document_paths[index] if index < 3 else note.data(Qt.ItemDataRole.UserRole) if index == 3 else "agent/HANDOFF.md" if index == 4 else "agent/STATUS.md"
        workspace = self.workspace()
        self.submit("定位 Obsidian 项目文档", lambda context: project_uri(workspace, item["id"], relative), self.launch_obsidian, persist_result=False)

    def open_overview(self, archived=None):
        # clicked(bool) must not override the currently selected filter.
        archived = self.filter.currentData() == "archived" if archived is None else archived
        workspace = self.workspace() if self.root() else None
        if workspace:
            self.submit("定位 Obsidian 项目索引", lambda context: index_uri(workspace, archived), self.launch_obsidian, persist_result=False)

    def refresh_indexes(self):
        workspace = self.workspace() if self.root() else None
        if workspace:
            self.submit("刷新 Obsidian 项目索引", workspace.refresh_indexes, lambda report: self.refresh())

    def create_project(self, legacy=None):
        if not self.root():
            self.choose_workspace()
            return
        dialog = ProjectDialog(self, legacy if isinstance(legacy, dict) else None)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, goal, entry = dialog.name.text().strip(), dialog.goal.toPlainText().strip(), dialog.entry.isChecked()
        workspace = self.workspace()
        def completed(report):
            self.search.clear()
            self.filter.setCurrentIndex(0)
            self.preferred_project = report["project_id"]
            self.refresh()
            QMessageBox.information(self, "项目已初始化", report["project_id"] + "\n" + report["directory"] + "\n\n已建立状态、任务和交接文件；项目默认为待开展。")
        self.submit("初始化新项目", lambda context: workspace.create(name, goal, entry, context), completed)

    def open_project(self):
        item = self.selected()
        if item:
            self.window.open_path(Path(item["directory"]))

    def append_log(self):
        item = self.selected()
        if not item:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(item["name"] + " · 写日志 / 交接")
        dialog.resize(610, 620)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("写在项目原来的日志文件里，Obsidian 看到同一份内容。"))
        fields = []
        for label, placeholder in [("完成了什么", "记录实际结果"), ("如何确认", "测试、检查结果或证据"), ("未完成 / 遇到的问题", "没有可以留空"), ("下一步", "下次从哪里继续")]:
            layout.addWidget(QLabel(label))
            text = QTextEdit()
            text.setPlaceholderText(placeholder)
            text.setMaximumHeight(100)
            layout.addWidget(text)
            fields.append((label, text))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            note = "\n\n".join("### " + label + "\n" + edit.toPlainText().strip() for label, edit in fields if edit.toPlainText().strip())
            if not note:
                return
            workspace = self.workspace()
            self.submit("追加项目日志与交接", lambda context: workspace.append_log(item["id"], note, context), lambda report: self.selection_changed(), persist_result=False)

    def edit_document(self):
        item = self.selected()
        index = self.documents.currentIndex()
        if not item or index > 1:
            return
        workspace = self.workspace()
        relative = self.document_paths[index]
        def loaded(report):
            dialog = QDialog(self)
            dialog.setWindowTitle(item["name"] + " · " + self.documents.tabText(index))
            dialog.resize(760, 590)
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("编辑原管理文档，保留 project_id；归档使用专用按钮，不复制第二份状态。"))
            editor = QTextEdit()
            editor.setPlainText(report["text"])
            layout.addWidget(editor)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
            buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                text = editor.toPlainText()
                self.submit("保存项目管理文档", lambda context: workspace.save_document(item["id"], relative, text, report["text"], context), lambda report: self.refresh(), persist_result=False)
        self.submit("读取待编辑管理文档", lambda context: workspace.document(item["id"], relative), loaded, persist_result=False)

    def move_project(self):
        item = self.selected()
        if not item:
            return
        if item.get("legacy"):
            self.create_project(legacy=item)
            return
        workspace = self.workspace()
        resume = item["path"].startswith("archive/")
        def preview(plan):
            dialog = QDialog(self)
            dialog.setWindowTitle("重新启用预检" if resume else "归档预检")
            dialog.resize(690, 460)
            layout = QVBoxLayout(dialog)
            report = QLabel(f"项目：{item['name']}（{item['id']}）\n\n从：{plan['source']}\n到：{plan['target']}\n\n已检查 {plan['file_count']} 个文件；保留编号并更新 Obsidian 入口和索引。")
            report.setWordWrap(True)
            layout.addWidget(report)
            reason = QTextEdit()
            reason.setPlaceholderText("重新启用后的下一步" if resume else "归档原因、交付物和遗留事项；未完成可以如实归档")
            layout.addWidget(reason)
            confirm = QCheckBox("已停止项目写入，并核对备份、遗留事项、旧路径及 Git / 同步设置")
            layout.addWidget(confirm)
            note = QLabel("归档只是移动，不是备份；备份未验证时照实保留记录。业务代码中的绝对路径不会自动改写。")
            note.setWordWrap(True)
            layout.addWidget(note)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText("重新启用" if resume else "归档项目")
            buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            def enabled():
                buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(confirm.isChecked() and bool(reason.toPlainText().strip()))
            confirm.toggled.connect(enabled)
            reason.textChanged.connect(enabled)
            buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                text = reason.toPlainText()
                self.pause_move_watches()
                self.move_selection = (item["id"], resume)
                try:
                    self.move_task = self.submit("重新启用项目" if resume else "归档项目", lambda context: workspace.move(plan, text, context)) or ""
                finally:
                    if not self.move_task:
                        self.restore_move_watches()
        self.submit("重新启用预检" if resume else "项目归档预检", lambda context: workspace.plan_move(item["id"], resume, context), preview, persist_result=False)

    def pause_move_watches(self):
        # Windows directory notification handles prevent rename. Today also
        # watches project STATUS files, including while its page is hidden.
        self.reload_timer.stop()
        self.document_version += 1
        self.note_version += 1
        for watcher in self.window.findChildren(QFileSystemWatcher):
            paths = watcher.files() + watcher.directories()
            self.move_watchers.append((watcher, paths, watcher.signalsBlocked()))
            watcher.setProperty("projectMovePaused", True)
            watcher.blockSignals(True)
            if paths:
                watcher.removePaths(paths)
        self.update_selection_actions()

    def restore_move_watches(self):
        for watcher, paths, blocked in self.move_watchers:
            watcher.setProperty("projectMovePaused", False)
            available = [p for p in paths if Path(p).exists()]
            if available:
                watcher.addPaths(available)
            watcher.blockSignals(blocked)
        self.move_watchers = []
        self.move_task = ""
        self.update_selection_actions()

    def move_finished(self, identity, state):
        if identity != self.move_task:
            return
        selection = self.move_selection
        self.move_selection = None
        self.restore_move_watches()
        if self.closed:
            return
        if state == "success" and selection:
            self.filter.setCurrentIndex(0 if selection[1] else 1)
            self.preferred_project = selection[0]
        self.refresh()
        self.window.today_page.refresh()
