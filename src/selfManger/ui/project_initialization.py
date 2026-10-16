"""Visual selections for the existing project creation dialog."""
from __future__ import annotations

import copy
from html import escape
import json
import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QTabWidget, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QPushButton, QTextEdit, QListWidget, QListWidgetItem, QFileDialog,
    QMessageBox, QTextBrowser, QLineEdit)

from ..agent_skills import discover_skills
from ..domain import UserError
from ..knowledge import KnowledgeIndex, knowledge_root
from ..runtime import TaskContext
from ..project_initialization import (BUILTIN_TEMPLATES, validate_selection, validate_template, load_template)
from .tasks import ReadWorker


class ProjectInitializationPanel(QTabWidget):
    def __init__(self, parent, window, preview_context):
        super().__init__(parent)
        self.window = window
        self.preview_context = preview_context
        self.workers = {}
        self.resource_items = {}
        self.templates = copy.deepcopy(list(BUILTIN_TEMPLATES))
        page = QWidget()
        layout = QVBoxLayout(page)
        toolbar = QHBoxLayout()
        self.template_choice = QComboBox()
        for template in self.templates:
            self.template_choice.addItem(template["name"])
        toolbar.addWidget(self.template_choice, 1)
        self.import_button = QPushButton("导入 JSON 模板")
        self.import_button.clicked.connect(self.import_template)
        toolbar.addWidget(self.import_button)
        export = QPushButton("另存为模板")
        export.clicked.connect(self.export_template)
        toolbar.addWidget(export)
        layout.addLayout(toolbar)
        hint = QLabel("开发规范将追加到 AGENTS.md；基础管理文档继续使用工作区模板。可直接编辑下方规范，自定义模板还可生成项目内文本文件。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        rules_toolbar = QHBoxLayout()
        rules_toolbar.addWidget(QLabel("项目开发规范"), 1)
        self.import_rules_button = QPushButton("选择规范 Markdown")
        self.import_rules_button.clicked.connect(self.import_rules)
        rules_toolbar.addWidget(self.import_rules_button)
        layout.addLayout(rules_toolbar)
        self.rules = QTextEdit()
        self.rules.setAcceptRichText(False)
        layout.addWidget(self.rules, 1)
        self.template_files = QLabel()
        self.template_files.setWordWrap(True)
        layout.addWidget(self.template_files)
        self.addTab(page, "开发规范")

        resources = QWidget()
        resource_layout = QVBoxLayout(resources)
        hint = QLabel("这一步可以跳过。勾选的 Skills 将完整复制到项目，可随项目迁移；知识资料保留引用。技能复制后，需按所用 Agent 的规则确认加载方式。")
        hint.setWordWrap(True)
        resource_layout.addWidget(hint)
        filter_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("筛选 Skills 和知识资源（隐藏的已勾选项仍保留）")
        self.search.textChanged.connect(self.filter_resources)
        filter_row.addWidget(self.search)
        resource_layout.addLayout(filter_row)
        columns = QHBoxLayout()
        self.skill_list = self._resource_column(columns, "已有 Skills", self.load_skills, self.choose_skill)
        self.knowledge_list = self._resource_column(columns, "知识库资源", self.load_knowledge, self.choose_knowledge)
        resource_layout.addLayout(columns, 1)
        self.resource_status = QLabel("可从已有知识索引选择；需要时点击刷新发现，或手动选择本地资源。")
        self.resource_status.setWordWrap(True)
        resource_layout.addWidget(self.resource_status)
        self.addTab(resources, "技能与资料（可选）")
        knowledge_page = getattr(window, "knowledge_page", None)
        self.add_knowledge(getattr(knowledge_page, "items", []))

        confirmation = QWidget()
        confirmation_layout = QVBoxLayout(confirmation)
        hint = QLabel("检查下面的项目内容。确认后点击“创建项目”；需要修改时可以返回上一步。")
        hint.setWordWrap(True)
        confirmation_layout.addWidget(hint)
        self.confirmation = QTextBrowser()
        confirmation_layout.addWidget(self.confirmation, 1)
        self.addTab(confirmation, "确认创建")
        self.confirmation_page = confirmation
        self.currentChanged.connect(self.refresh_confirmation)
        self.template_choice.currentIndexChanged.connect(self.select_template)
        self.select_template(0)

    def _resource_column(self, columns, title, refresh, choose):
        column = QVBoxLayout()
        column.addWidget(QLabel(title))
        listing = QListWidget()
        listing.setWordWrap(True)
        column.addWidget(listing, 1)
        buttons = QHBoxLayout()
        button = QPushButton("发现已有 Skills" if title == "已有 Skills" else "刷新知识索引")
        button.clicked.connect(refresh)
        buttons.addWidget(button)
        choose_button = QPushButton("选择目录" if title == "已有 Skills" else "选择文件")
        choose_button.clicked.connect(choose)
        buttons.addWidget(choose_button)
        column.addLayout(buttons)
        columns.addLayout(column, 1)
        return listing

    def select_template(self, index):
        template = self.templates[index]
        self.rules.setPlainText(template["rules"])
        self.template_files.setText("模板附加文件：" + ("、".join(template["files"]) or "无（只生成基础管理文档与小年配置）"))

    def selection(self):
        def checked(listing):
            return [listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(listing.count())
                    if listing.item(i).checkState() == Qt.CheckState.Checked]
        return validate_selection({"template": self.templates[self.template_choice.currentIndex()],
                                   "rules": self.rules.toPlainText(), "skills": checked(self.skill_list),
                                   "knowledge": checked(self.knowledge_list)})

    def _add_resource(self, listing, entry, checked=False):
        location = entry.get("path", "")
        key = (id(listing), os.path.normcase(location) if location else entry.get("url", ""))
        if key in self.resource_items:
            if checked:
                self.resource_items[key].setCheckState(Qt.CheckState.Checked)
            return
        suffix = " · " + entry["agent"] if entry.get("agent") else ""
        item = QListWidgetItem(entry["name"] + suffix)
        item.setData(Qt.ItemDataRole.UserRole, entry)
        item.setToolTip(entry.get("path") or entry.get("url", ""))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        listing.addItem(item)
        self.resource_items[key] = item
        query = self.search.text().strip().casefold()
        item.setHidden(bool(query and query not in (item.text() + item.toolTip()).casefold()))

    def add_knowledge(self, entries):
        for entry in entries:
            if entry.get("missing") or entry.get("deleted") or entry.get("archived"):
                continue
            if entry.get("path") or entry.get("source_url"):
                self._add_resource(self.knowledge_list, {
                    "id": str(entry.get("relative", entry.get("id", ""))), "name": str(entry.get("name", "资源")),
                    "path": str(entry.get("path", "")), "url": str(entry.get("source_url", "")), "agent": ""})

    def filter_resources(self, *_):
        query = self.search.text().strip().casefold()
        for listing in (self.skill_list, self.knowledge_list):
            for i in range(listing.count()):
                item = listing.item(i)
                item.setHidden(bool(query and query not in (item.text() + item.toolTip()).casefold()))

    def _start_read(self, key, operation):
        if key in self.workers:
            return
        worker = ReadWorker(operation)
        self.workers[key] = worker
        # Bound QObject methods disconnect automatically if the dialog is destroyed.
        worker.signals.finished.connect(self.skills_finished if key == "skills" else self.knowledge_finished)
        self.resource_status.setText("正在读取已有资源…")
        self.window.pool.start(worker)

    def load_skills(self):
        agents = [r for r in self.window.store.resources() if r.kind in {"agent", "hermes_local"}]
        def observe(cancel):
            result, errors = [], []
            for agent in agents:
                if cancel.is_set():
                    break
                try:
                    for skill in discover_skills(agent):
                        result.append({"id": agent.id + ":" + skill.name, "name": skill.name,
                                       "path": str(skill.path), "url": "", "agent": agent.name})
                except (UserError, OSError) as exc:
                    errors.append(agent.name + "：" + str(exc))
            return {"items": result, "errors": errors}
        self._start_read("skills", observe)

    def skills_finished(self, result):
        self.workers.pop("skills", None)
        if result is None:
            self.resource_status.setText("Skills 发现失败，请检查登记目录，或手动选择已有 Skill。")
            return
        for entry in result["items"]:
            self._add_resource(self.skill_list, entry)
        self.resource_status.setText(f"发现 {len(result['items'])} 个已有 Skills。" + "；".join(result["errors"]))

    def load_knowledge(self):
        root = knowledge_root(self.window.store)
        if root is None:
            self.resource_status.setText("请先在设置中选择知识库，或直接选择本地知识文件。")
            return
        store = self.window.store
        def observe(cancel):
            if cancel.is_set():
                return None
            context = TaskContext()
            context.cancel = cancel
            return KnowledgeIndex(store, root).scan(context)
        self._start_read("knowledge", observe)

    def knowledge_finished(self, result):
        self.workers.pop("knowledge", None)
        if result is None:
            self.resource_status.setText("知识索引读取失败，可手动选择本地文件。")
            return
        self.add_knowledge(result["items"])
        self.resource_status.setText(f"已载入 {len(result['items'])} 个知识索引项。" + "；".join(result["errors"][:3]))

    def choose_skill(self):
        directory = QFileDialog.getExistingDirectory(self, "选择已有 Skill 目录（包含 SKILL.md）")
        if directory:
            if not (Path(directory) / "SKILL.md").is_file():
                QMessageBox.information(self, "Skill 不可用", "请选择包含 SKILL.md 的已有 Skill 目录。")
                return
            self._add_resource(self.skill_list, {"id": "manual", "name": Path(directory).name,
                               "path": directory, "url": "", "agent": "手动指定"}, True)

    def choose_knowledge(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择要引用的已有知识文件")
        for path in paths:
            self._add_resource(self.knowledge_list, {"id": "manual", "name": Path(path).name,
                               "path": path, "url": "", "agent": ""}, True)

    def import_template(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入初始化模板", "", "JSON 模板 (*.json)")
        if not path:
            return
        try:
            template = load_template(Path(path))
        except (UserError, OSError) as exc:
            QMessageBox.warning(self, "模板不可用", str(exc))
            return
        self.templates.append(template)
        self.template_choice.addItem(template["name"] + "（自定义）")
        self.template_choice.setCurrentIndex(len(self.templates) - 1)

    def import_rules(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择开发规范", "", "Markdown / 文本 (*.md *.txt)")
        if not path:
            return
        try:
            source = Path(path)
            if not source.is_file() or source.stat().st_size > 400_000:
                raise UserError("规范文件需要是最多 100000 字符的 UTF-8 文本（文件不超过 400 KB）。")
            text = source.read_text(encoding="utf-8-sig")
            validate_selection({"rules": text})
            self.rules.setPlainText(text)
        except (UserError, OSError, UnicodeError) as exc:
            QMessageBox.warning(self, "规范未载入", str(exc))

    def export_template(self):
        try:
            template = dict(self.templates[self.template_choice.currentIndex()], rules=self.rules.toPlainText())
            template = validate_template(template)
            path, _ = QFileDialog.getSaveFileName(self, "另存当前模板（选择新文件）", "project-template.json", "JSON 模板 (*.json)")
            if path:
                with Path(path).open("x", encoding="utf-8") as output:
                    output.write(json.dumps(template, ensure_ascii=False, indent=2) + "\n")
        except (UserError, OSError) as exc:
            QMessageBox.warning(self, "模板未保存", str(exc))

    def refresh_confirmation(self, *_):
        if self.currentWidget() is not self.confirmation_page:
            return
        try:
            directory, values = self.preview_context()
            selection = self.selection()
        except UserError as exc:
            self.confirmation.setPlainText("请返回检查初始化选项：\n" + str(exc))
            return
        def items(entries, empty):
            return ("<ul>" + "".join("<li>" + escape(item["name"]) + "</li>" for item in entries) + "</ul>") if entries else "<p>" + empty + "</p>"
        template = selection["template"]
        text = ("<h2>" + escape(values["NAME"]) + "</h2><p>" + escape(values["GOAL"]).replace("\n", "<br>")
                + "</p><h3>保存位置</h3><p>" + escape(str(directory))
                + "</p><p>项目编号由系统分配，现有项目不会被覆盖。</p>"
                + "<h3>开发规范</h3><p>" + escape(template["name"]) + " · 将保存你编辑的开发规范，供 AI 接手时阅读。</p>"
                + f"<h3>复制到项目的 Skills（{len(selection['skills'])} 项）</h3>"
                + items(selection["skills"], "未选择。以后可按需补充。")
                + f"<h3>关联知识资料（{len(selection['knowledge'])} 项）</h3>"
                + items(selection["knowledge"], "未选择。以后可按需补充。")
                + "<h3>创建后即可使用</h3><ul><li>项目说明、开发规范、任务清单与交接文档。</li>"
                + "<li>新项目初始化和后续接手的 AI 提示词。</li>"
                + "<li>所选技能的完整副本，以及知识资料的引用清单。</li></ul>")
        if template["files"]:
            text += "<h3>自定义模板附加文件</h3><ul>" + "".join("<li>" + escape(path) + "</li>" for path in template["files"]) + "</ul>"
        text += "<p>只创建本地文件，不运行技能脚本；技能是否自动加载由所用 Agent 决定。</p>"
        self.confirmation.setHtml(text)

    def cancel_reads(self):
        for worker in self.workers.values():
            worker.cancel.set()
