from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, QEvent, QFileSystemWatcher
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QLabel, QPushButton,
    QComboBox, QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSplitter, QTextBrowser, QDialog, QDialogButtonBox, QFormLayout, QTextEdit, QCheckBox,
    QFileDialog, QMessageBox, QListWidget, QListWidgetItem, QInputDialog, QToolButton, QMenu)

from ..domain import Resource, UserError
from ..project_workspaces import ProjectWorkspace, STATES
from ..obsidian import project_uri, index_uri
from .components import FlowLayout


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
    def __init__(self, parent):
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
        self.preferred_note = ""
        self.closed = False
        layout = QVBoxLayout(self)
        title = QLabel("本地项目")
        title.setObjectName("Title")
        layout.addWidget(title)
        note = QLabel("按编号规范创建项目，维护状态、任务和日志；需要时归档或重新启用。")
        note.setWordWrap(True)
        note.setObjectName("Subtitle")
        layout.addWidget(note)
        lifecycle = QWidget()
        body = QVBoxLayout(lifecycle)
        body.setContentsMargins(0, 8, 0, 0)
        root_row = QHBoxLayout()
        self.root_label = QLabel()
        self.root_label.setWordWrap(True)
        self.root_label.setObjectName("Subtitle")
        root_row.addWidget(self.root_label, 1)
        self.obsidian_menu = QToolButton()
        self.obsidian_menu.setText("Obsidian 总览")
        self.obsidian_menu.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.obsidian_menu.clicked.connect(lambda: self.open_overview())
        menu = QMenu(self.obsidian_menu)
        menu.addAction("打开项目总览", lambda: self.open_overview(False))
        menu.addAction("打开归档索引", lambda: self.open_overview(True))
        menu.addSeparator()
        menu.addAction("刷新 Obsidian 索引", self.refresh_indexes)
        self.obsidian_menu.setMenu(menu)
        body.addLayout(root_row)
        root_actions = FlowLayout()
        root_actions.addWidget(self.obsidian_menu)
        choose = QPushButton("选择工作区…")
        choose.clicked.connect(self.choose_workspace)
        root_actions.addWidget(choose)
        self.myself_status = QLabel()
        self.myself_status.setWordWrap(True)
        root_actions.addWidget(self.myself_status)
        for label, action in [("备份 myself", "backup"), ("恢复 myself", "restore"), ("myself 配置", "edit")]:
            control = QPushButton(label)
            control.clicked.connect(lambda checked=False, action=action: self.myself_action(action))
            root_actions.addWidget(control)
        body.addLayout(root_actions)
        toolbar = QHBoxLayout()
        self.new_button = QPushButton("新建项目")
        self.new_button.setProperty("primary", True)
        self.new_button.clicked.connect(self.create_project)
        toolbar.addWidget(self.new_button)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索编号或项目名称")
        self.search.textChanged.connect(self.render)
        toolbar.addWidget(self.search, 1)
        self.filter = QComboBox()
        for text, value in [("未归档", "current"), ("已归档", "archived"), ("全部", "all")]:
            self.filter.addItem(text, value)
        self.filter.currentIndexChanged.connect(self.render)
        toolbar.addWidget(self.filter)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        toolbar.addWidget(refresh)
        body.addLayout(toolbar)
        actions = FlowLayout()
        self.open_button = QPushButton("打开文件夹")
        self.open_button.clicked.connect(self.open_project)
        self.obsidian_button = QPushButton("Obsidian 中打开")
        self.obsidian_button.setToolTip("打开当前状态、任务、日志，或所选笔记的原文件")
        self.obsidian_button.clicked.connect(self.open_in_obsidian)
        self.log_button = QPushButton("写日志")
        self.log_button.clicked.connect(self.append_log)
        self.edit_button = QPushButton("编辑当前文档")
        self.edit_button.clicked.connect(self.edit_document)
        self.move_button = QPushButton("归档项目…")
        self.move_button.clicked.connect(self.move_project)
        for widget in (self.open_button, self.obsidian_button, self.log_button, self.edit_button, self.move_button):
            actions.addWidget(widget)
        body.addLayout(actions)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["编号", "项目", "状态"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumWidth(285)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        split.addWidget(self.table)
        self.documents = QTabWidget()
        self.views = []
        self.document_paths = ["agent/STATUS.md", "agent/TASKS.md", "agent/HANDOFF.md"]
        for name in ("当前状态", "任务入口", "日志与交接"):
            view = QTextBrowser()
            view.setOpenExternalLinks(True)
            if name == "日志与交接":
                self.log_view = ProjectLogView(view)
                self.documents.addTab(self.log_view, "项目日志")
            else:
                self.documents.addTab(view, name)
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
        self.documents.addTab(notes_page, "项目笔记")
        self.documents.currentChanged.connect(self.selection_changed)
        split.addWidget(self.documents)
        split.setSizes([340, 620])
        body.addWidget(split, 1)
        self.message = QLabel("请选择已有工作区，或选择一个空文件夹初始化。")
        self.message.setObjectName("Subtitle")
        self.message.setWordWrap(True)
        body.addWidget(self.message)
        layout.addWidget(lifecycle, 1)
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
        QTimer.singleShot(0, self.refresh)

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
        # Legacy/restored registrations remain accessible without a second project tab.
        dialog = QDialog(self)
        dialog.setWindowTitle("资料备份 · " + resource.name)
        dialog.resize(1000, 680)
        layout = QVBoxLayout(dialog)
        layout.addWidget(self.backup_page)
        self.backup_page.show()
        row = next((i for i, r in enumerate(self.backup_page.rows) if r.id == resource.id), 0)
        self.backup_page.resources_table.selectRow(row)
        dialog.exec()
        self.backup_page.setParent(self)
        self.backup_page.hide()

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
        self.window.submit(owner, title, locked, callback, persist_result=persist_result)

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
        if self.closed:
            return
        root = self.root()
        self.root_label.setText("工作区：" + str(root) if root else "未选择项目工作区")
        self.new_button.setEnabled(bool(root))
        self.obsidian_menu.setEnabled(bool(root))
        if not root:
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
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        for row, item in enumerate(self.rows):
            for column, value in enumerate((item["id"], item["name"], STATES.get(item["state"], "请检查状态"))):
                cell = QTableWidgetItem(value)
                cell.setToolTip(item.get("error") or item["directory"])
                self.table.setItem(row, column, cell)
        if self.rows:
            self.table.selectRow(next((i for i, p in enumerate(self.rows) if selected and p["id"] == selected["id"]), 0))
        self.table.blockSignals(False)
        self.selection_changed()

    def selection_changed(self):
        item = self.selected()
        enabled = bool(item)
        archived = bool(item and item["path"].startswith("archive/"))
        self.open_button.setEnabled(enabled)
        self.obsidian_button.setEnabled(enabled and self.documents.currentIndex() < 3)
        self.new_note_button.setEnabled(enabled and not archived)
        self.log_button.setEnabled(enabled and not archived)
        self.edit_button.setEnabled(enabled and not archived and self.documents.currentIndex() < 2)
        self.move_button.setEnabled(enabled and not item.get("legacy", False) if item else False)
        self.move_button.setText("重新启用…" if archived else "归档项目…")
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
        if not item:
            self.watch_paths = []
            self.update_watches([])
            return
        root, identity = self.root(), item["id"]
        def read(context):
            workspace = ProjectWorkspace(root)
            documents = [workspace.document(identity, relative) for relative in self.document_paths]
            notes = workspace.notes(identity, context)["notes"]
            paths = [str(workspace.registry_path), str(workspace.registry_path.parent), *[d["path"] for d in documents], str(workspace.path(item["path"] + "/agent")), str(workspace.path(item["path"])), str(workspace.path(item["path"] + "/笔记"))]
            return {"documents": documents, "notes": notes, "watch_paths": paths}
        def show(report):
            if version != self.document_version:
                return
            for view, document in zip(self.views, report["documents"]):
                view.document().setBaseUrl(QUrl.fromLocalFile(str(Path(document["path"]).parent) + os.sep))
                if view is self.views[2]:
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
        self.obsidian_button.setEnabled(bool(item) and (self.documents.currentIndex() < 3 or bool(note)))
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
        relative = self.document_paths[index] if index < 3 else note.data(Qt.ItemDataRole.UserRole)
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

    def create_project(self):
        dialog = ProjectDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, goal, entry = dialog.name.text().strip(), dialog.goal.toPlainText().strip(), dialog.entry.isChecked()
        workspace = self.workspace()
        def completed(report):
            self.filter.setCurrentIndex(0)
            self.refresh()
            QMessageBox.information(self, "项目已初始化", report["project_id"] + "\n" + report["directory"] + "\n\n已建立状态、任务和交接文件；项目默认为待开展。")
        self.submit("初始化新项目", lambda context: workspace.create(name, goal, entry, context), completed)

    def open_project(self):
        item = self.selected()
        if item:
            QDesktopServices.openUrl(QUrl.fromLocalFile(item["directory"]))

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
                self.submit("重新启用项目" if resume else "归档项目", lambda context: workspace.move(plan, text, context), lambda report: self.refresh())
        self.submit("重新启用预检" if resume else "项目归档预检", lambda context: workspace.plan_move(item["id"], resume, context), preview, persist_result=False)
