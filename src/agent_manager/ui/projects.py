from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QLabel, QPushButton,
    QComboBox, QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSplitter, QTextBrowser, QDialog, QDialogButtonBox, QFormLayout, QTextEdit, QCheckBox,
    QFileDialog, QMessageBox)

from ..domain import Resource, UserError
from ..project_workspaces import ProjectWorkspace, STATES


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
        layout = QVBoxLayout(self)
        title = QLabel("本地项目")
        title.setObjectName("Title")
        layout.addWidget(title)
        note = QLabel("按编号规范创建项目，维护状态、任务和日志；需要时归档或重新启用。")
        note.setWordWrap(True)
        note.setObjectName("Subtitle")
        layout.addWidget(note)
        self.tabs = QTabWidget()
        lifecycle = QWidget()
        body = QVBoxLayout(lifecycle)
        body.setContentsMargins(0, 8, 0, 0)
        root_row = QHBoxLayout()
        self.root_label = QLabel()
        self.root_label.setWordWrap(True)
        self.root_label.setObjectName("Subtitle")
        root_row.addWidget(self.root_label, 1)
        choose = QPushButton("选择工作区…")
        choose.clicked.connect(self.choose_workspace)
        root_row.addWidget(choose)
        body.addLayout(root_row)
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
        actions = QHBoxLayout()
        self.open_button = QPushButton("打开项目")
        self.open_button.clicked.connect(self.open_project)
        self.log_button = QPushButton("写日志 / 交接")
        self.log_button.clicked.connect(self.append_log)
        self.edit_button = QPushButton("编辑当前文档")
        self.edit_button.clicked.connect(self.edit_document)
        self.move_button = QPushButton("归档项目…")
        self.move_button.clicked.connect(self.move_project)
        for widget in (self.open_button, self.log_button, self.edit_button, self.move_button):
            actions.addWidget(widget)
        actions.addStretch()
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
            self.documents.addTab(view, name)
            self.views.append(view)
        self.documents.currentChanged.connect(self.selection_changed)
        split.addWidget(self.documents)
        split.setSizes([340, 620])
        body.addWidget(split, 1)
        self.message = QLabel("请选择已有工作区，或选择一个空文件夹初始化。")
        self.message.setObjectName("Subtitle")
        self.message.setWordWrap(True)
        body.addWidget(self.message)
        self.tabs.addTab(lifecycle, "项目管理")
        self.tabs.addTab(backup_page, "资料备份与 Obsidian")
        layout.addWidget(self.tabs)
        self.project_rows = []
        self.render()
        QTimer.singleShot(0, self.refresh)

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
        root = self.root()
        self.root_label.setText("工作区：" + str(root) if root else "未选择项目工作区")
        self.new_button.setEnabled(bool(root))
        if not root:
            return
        def loaded(report):
            self.project_rows = report["projects"]
            self.render()
            self.message.setText(f"{len(self.project_rows)} 个项目 · 状态来自项目 STATUS，日志保存在 HANDOFF。归档不等于备份。")
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
        self.log_button.setEnabled(enabled and not archived)
        self.edit_button.setEnabled(enabled and not archived and self.documents.currentIndex() < 2)
        self.move_button.setEnabled(enabled and not item.get("legacy", False) if item else False)
        self.move_button.setText("重新启用…" if archived else "归档项目…")
        self.document_version += 1
        version = self.document_version
        for view in self.views:
            view.setPlainText("选择项目查看管理资料。" if not item else "读取中…")
        if not item:
            return
        root, identity = self.root(), item["id"]
        def read(context):
            workspace = ProjectWorkspace(root)
            return {"documents": [workspace.document(identity, relative) for relative in self.document_paths]}
        def show(report):
            if version != self.document_version:
                return
            for view, document in zip(self.views, report["documents"]):
                view.document().setBaseUrl(QUrl.fromLocalFile(str(Path(document["path"]).parent) + os.sep))
                view.setMarkdown(document["text"])
        # A dedicated read does not compete with the selected lifecycle task's resource identity.
        self.window.submit(None, "读取项目管理文档", read, show, persist_result=False)

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
        dialog.resize(610, 430)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("追加到 HANDOFF，保留历史；请记录实际结果、验证、未完成项和下一步。"))
        text = QTextEdit()
        text.setPlaceholderText("已做：\n验证：\n未完成 / 阻塞：\n下一步：")
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            note = text.toPlainText()
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
