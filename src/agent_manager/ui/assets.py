from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QSplitter,
    QTabBar, QTextEdit, QVBoxLayout, QWidget, QTableWidgetItem)

from .. import asset_library as assets
from ..domain import Resource, UserError
from .components import FlowLayout


class AssetPage(QWidget):
    def __init__(self, window):
        super().__init__()
        from .window import button, table, text_view
        self.window = window
        self.items, self.scan_job = [], ""
        self.generation = 0
        layout = QVBoxLayout(self)
        title = QLabel("技能与工具")
        title.setObjectName("Title")
        layout.addWidget(title)
        hint = QLabel("统一查看各 Agent 的技能、MCP 连接和提示词。通用库可随便携程序带走；复制技能前选择要给谁用。")
        hint.setWordWrap(True)
        hint.setObjectName("Subtitle")
        layout.addWidget(hint)
        actions = FlowLayout()
        self.refresh_button = button("刷新清单", self.refresh)
        actions.addWidget(self.refresh_button)
        actions.addWidget(button("添加技能目录", self.add_root))
        self.import_button = button("导入技能", self.import_skill, True)
        actions.addWidget(self.import_button)
        self.copy_button = button("复制技能给 Agent", self.deploy_skill)
        actions.addWidget(self.copy_button)
        self.prompt_button = button("新建提示词", lambda: self.edit_prompt(False))
        actions.addWidget(self.prompt_button)
        self.edit_button = button("编辑提示词", lambda: self.edit_prompt(True))
        actions.addWidget(self.edit_button)
        self.mcp_button = button("添加 MCP 文件", self.add_mcp_file)
        actions.addWidget(self.mcp_button)
        actions.addWidget(button("管理自选来源", self.manage_sources))
        actions.addWidget(button("备份通用库", self.backup_library))
        layout.addLayout(actions)
        self.categories = QTabBar()
        self.categories.addTab("技能（Skills）")
        self.categories.addTab("MCP（外部工具）")
        self.categories.addTab("提示词")
        self.categories.currentChanged.connect(self.filter_items)
        layout.addWidget(self.categories)
        bar = QHBoxLayout()
        self.scope = QComboBox()
        self.scope.addItem("全部来源", "")
        self.scope.setMaximumWidth(250)
        self.scope.currentIndexChanged.connect(self.filter_items)
        bar.addWidget(self.scope)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称或来源")
        self.search.textChanged.connect(self.filter_items)
        bar.addWidget(self.search, 1)
        layout.addLayout(bar)
        split = QSplitter()
        self.table = table(["名称", "来源", "文件"])
        self.table.setMinimumWidth(300)
        self.table.itemSelectionChanged.connect(self.selected)
        split.addWidget(self.table)
        detail = QWidget()
        body = QVBoxLayout(detail)
        body.setContentsMargins(0, 0, 0, 0)
        self.path = QLabel("选中左侧条目查看")
        self.path.setWordWrap(True)
        self.path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self.path)
        self.preview = text_view()
        body.addWidget(self.preview, 1)
        controls = FlowLayout()
        self.copy_text_button = button("复制内容", lambda: QApplication.clipboard().setText(self.preview.toPlainText()))
        controls.addWidget(self.copy_text_button)
        self.open_button = button("打开所在目录", self.open_selected)
        controls.addWidget(self.open_button)
        body.addLayout(controls)
        split.addWidget(detail)
        split.setSizes([520, 430])
        layout.addWidget(split, 1)
        self.status = QLabel("点击“刷新清单”，读取已登记的本地 Agent。支持自选技能目录，不自动下载或启用。")
        self.status.setWordWrap(True)
        self.status.setObjectName("Subtitle")
        layout.addWidget(self.status)
        self.filter_items()

    def current(self):
        cell = self.table.item(self.table.currentRow(), 0)
        return cell.data(Qt.ItemDataRole.UserRole) if cell else None

    def refresh(self):
        if self.scan_job in self.window.jobs:
            return
        def completed(report):
            self.items = report["items"]
            previous = self.scope.currentData()
            self.scope.blockSignals(True)
            self.scope.clear()
            self.scope.addItem("全部来源", "")
            for owner in sorted({item["owner"] for item in self.items}):
                self.scope.addItem(owner, owner)
            self.scope.setCurrentIndex(max(0, self.scope.findData(previous)))
            self.scope.blockSignals(False)
            self.status.setText(f"共 {len(self.items)} 项。" + (" ".join(report["notes"]) or "MCP 只查看连接说明；安装依赖、启用连接在原 Agent 中完成。"))
            self.filter_items()
        self.scan_job = self.window.submit(None, "读取技能、工具与提示词清单", lambda context: assets.scan(self.window.store, context), completed, persist_result=False)

    def filter_items(self, *_):
        if not hasattr(self, "table"):
            return
        category = ["skill", "mcp", "prompt"][self.categories.currentIndex()]
        query, owner = self.search.text().casefold(), self.scope.currentData()
        rows = [item for item in self.items if item["category"] == category and (not owner or item["owner"] == owner) and query in (item["name"] + item["owner"]).casefold()]
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for row, item in enumerate(rows):
            for column, value in enumerate([item["name"], item["owner"], item["path"]]):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                cell.setData(Qt.ItemDataRole.UserRole, item)
                self.table.setItem(row, column, cell)
        self.table.clearSelection()
        self.table.setCurrentCell(-1, -1)
        self.table.blockSignals(False)
        self.import_button.setVisible(category == "skill")
        self.copy_button.setVisible(category == "skill")
        self.prompt_button.setVisible(category == "prompt")
        self.edit_button.setVisible(category == "prompt")
        self.mcp_button.setVisible(category == "mcp")
        self.selected()

    def selected(self):
        self.generation += 1
        generation, item = self.generation, self.current()
        self.copy_button.setEnabled(bool(item and item["category"] == "skill"))
        self.edit_button.setEnabled(bool(item and item["category"] == "prompt" and item["managed"]))
        self.open_button.setEnabled(bool(item))
        self.copy_text_button.setEnabled(False)
        self.path.setText(item["path"] if item else "选中左侧条目查看")
        explanation = ["选择技能查看内容。没有找到时可以刷新或添加技能目录；导入技能请选择直接包含 SKILL.md 的文件夹。",
                       "MCP 是 Agent 连接外部工具的配置。没有找到时可添加 JSON、TOML 或 YAML 文件；这里查看连接清单，启用和修改在原 Agent 中完成。",
                       "提示词是你反复使用的工作要求。点击“新建提示词”保存到通用库，再复制到各 Agent 使用。原 Agent 的文件只供查看，通用库里的提示词可以编辑。"]
        self.preview.setPlainText("读取中…" if item else explanation[self.categories.currentIndex()])
        if item:
            def completed(report):
                if generation == self.generation:
                    self.preview.setPlainText(report["text"])
                    self.copy_text_button.setEnabled(True)
            self.window.submit(None, "查看通用资料", lambda context: assets.preview(self.window.store, item, context), completed, persist_result=False)

    def add_root(self):
        directory = QFileDialog.getExistingDirectory(self, "选择包含多个技能文件夹的目录")
        if not directory:
            return
        name, okay = QInputDialog.getText(self, "技能来源", "给这个目录起一个容易认的名字", text=Path(directory).name)
        if not okay or not name.strip():
            return
        roots = self.window.store.setting("asset_roots", [])
        if len(roots) >= 20:
            QMessageBox.information(self, "目录数量", "最多添加 20 个自选来源。")
            return
        try:
            assets.checked(Path(directory))
            self.window.store.set_setting("asset_roots", [*roots, {"owner": name.strip()[:120], "path": directory, "skills_only": True}])
            self.refresh()
        except UserError as exc:
            QMessageBox.warning(self, "目录未添加", str(exc))

    def copy_skill(self, source, destination):
        def operation(context):
            with self.window.service.locks.acquire(["path:" + str(source.resolve()), "path:" + str(destination.resolve())]):
                return assets.copy_skill(source, destination, context)
        def completed(report):
            self.refresh()
            QMessageBox.information(self, "技能已复制", report["note"] + "\n\n" + report["target"])
        self.window.submit(None, "复制技能文件", operation, completed)

    def add_mcp_file(self):
        file = QFileDialog.getOpenFileName(self, "选择已有 MCP 配置文件", "", "连接配置 (*.json *.toml *.yaml *.yml)")[0]
        if not file:
            return
        files = self.window.store.setting("asset_mcp_files", [])
        if file not in files and len(files) < 20:
            self.window.store.set_setting("asset_mcp_files", [*files, file])
        self.refresh()

    def manage_sources(self):
        roots = self.window.store.setting("asset_roots", [])
        files = self.window.store.setting("asset_mcp_files", [])
        labels = [root["owner"] + " · " + root["path"] for root in roots] + ["MCP · " + file for file in files]
        if not labels:
            QMessageBox.information(self, "自选来源", "没有自选来源。已登记 Agent 的目录会自动显示；可以添加其他技能目录或 MCP 文件。")
            return
        name, okay = QInputDialog.getItem(self, "移除自选来源", "只移除这里的登记，磁盘文件保留", labels, editable=False)
        if okay:
            index = labels.index(name)
            if index < len(roots):
                self.window.store.set_setting("asset_roots", [root for row, root in enumerate(roots) if row != index])
            else:
                self.window.store.set_setting("asset_mcp_files", [file for row, file in enumerate(files) if row != index - len(roots)])
            self.refresh()

    def import_skill(self):
        directory = QFileDialog.getExistingDirectory(self, "选择直接包含 SKILL.md 的技能文件夹")
        if directory:
            self.copy_skill(Path(directory), assets.library_root(self.window.store) / "skills")

    def deploy_skill(self):
        item = self.current()
        if not item or item["category"] != "skill":
            return
        source = Path(item["path"]).parent
        dialog = QDialog(self)
        dialog.setWindowTitle("把技能复制给谁用")
        dialog.resize(650, 300)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("技能：" + item["name"]))
        choices = QComboBox()
        for root in assets.roots_for(self.window.store):
            if not root["managed"] and Path(root["path"]).is_dir():
                target = Path(root["path"]) if root.get("skills_only") else Path(root["path"]) / "skills"
                if target != source.parent:
                    choices.addItem(root["owner"], str(target))
        layout.addWidget(choices)
        destination = QTextEdit()
        destination.setReadOnly(True)
        destination.setMaximumHeight(80)
        layout.addWidget(destination)
        def select():
            destination.setPlainText(choices.currentData() or "")
        choices.currentIndexChanged.connect(select)
        select()
        choose = QPushButton("选择其他 Agent 的技能目录")
        def custom():
            value = QFileDialog.getExistingDirectory(dialog, "选择目标 Agent 的 skills 目录")
            if value:
                choices.addItem("自选目录", value)
                choices.setCurrentIndex(choices.count() - 1)
        choose.clicked.connect(custom)
        layout.addWidget(choose)
        note = QLabel("同名技能会保留，不覆盖。复制后请在原 Agent 中确认支持；管家不执行技能中的脚本。")
        note.setWordWrap(True)
        layout.addWidget(note)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        controls.button(QDialogButtonBox.StandardButton.Ok).setText("复制技能")
        controls.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        controls.accepted.connect(dialog.accept)
        controls.rejected.connect(dialog.reject)
        layout.addWidget(controls)
        if dialog.exec() == QDialog.DialogCode.Accepted and choices.currentData():
            self.copy_skill(source, Path(choices.currentData()))

    def edit_prompt(self, editing):
        item = self.current() if editing else None
        original = None
        if editing and (not item or not item["managed"]):
            return
        try:
            if item:
                original = assets.text_file(Path(item["path"]))
        except UserError as exc:
            QMessageBox.warning(self, "无法读取", str(exc))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑通用提示词" if editing else "新建通用提示词")
        dialog.resize(700, 530)
        layout = QVBoxLayout(dialog)
        name = QLineEdit(item["name"] if item else "")
        name.setPlaceholderText("名称，例如：每次开始工作")
        name.setReadOnly(editing)
        layout.addWidget(name)
        text = QTextEdit()
        text.setPlainText(original or "")
        layout.addWidget(text, 1)
        hint = QLabel("保存到管家通用库。复制内容后可在各 Agent 使用；不会自动改写原 Agent 的提示词。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        controls.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        controls.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        controls.rejected.connect(dialog.reject)
        def save():
            try:
                with self.window.service.locks.acquire(["path:" + str(assets.library_root(self.window.store).resolve())]):
                    assets.save_prompt(self.window.store, name.text(), text.toPlainText(), original)
                dialog.accept()
                self.refresh()
            except (UserError, OSError) as exc:
                QMessageBox.warning(dialog, "未保存", str(exc) if isinstance(exc, UserError) else "目录不可写，请检查权限。")
        controls.accepted.connect(save)
        layout.addWidget(controls)
        dialog.exec()

    def open_selected(self):
        item = self.current()
        if item:
            self.window.open_path(Path(item["path"]).parent)

    def backup_library(self):
        root = assets.library_root(self.window.store)
        if not root.is_dir():
            QMessageBox.information(self, "通用库还没有资料", "先导入技能或新建提示词，再创建备份。")
            return
        resource = next((item for item in self.window.store.resources() if item.id == "agentassetslibrary"), None)
        if resource is None:
            resource = Resource("管家 · 技能与提示词库", "agent", {"path": str(root), "engine": "其他 Agent"}, "agentassetslibrary")
        resource.options["path"] = str(root)
        self.window.store.save_resource(resource)
        self.window.refresh_resources()
        self.window.perform(resource, "backup")
