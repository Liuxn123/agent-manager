from __future__ import annotations

import json
import os
import html
import time
from pathlib import Path
from uuid import uuid4
from datetime import datetime

from PySide6.QtCore import QThreadPool, QTimer, Qt, QUrl, QUrlQuery, QSize
from PySide6.QtGui import QDesktopServices, QColor, QTextOption
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QMainWindow, QMenu, QMessageBox, QPushButton, QProgressBar, QSplitter, QStackedWidget,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget, QScrollArea,
    QTabWidget, QListWidgetItem, QSpinBox, QToolButton, QFormLayout, QSizePolicy, QComboBox, QApplication)

from .. import __version__, archives
from ..application import ApplicationService
from ..domain import KINDS, Resource, RestorePlan, UserError
from ..security import safe_result
from ..storage import Store
from .dialogs import ArchiveRestoreDialog, PasswordDialog, PlanDialog, ResourceDialog, RecordsDialog, TransferDialog, choose_path
from ..profiles import restored_resource, sources_for
from .tasks import Worker
from .presentation import ACTION_LABELS, readable_report, readable_time, readable_size
from .components import nav_icon, card, FlowLayout, ReadableTable
from ..maintenance import backup_health, is_due, fingerprint, age_hours
from ..storage import now

STATE_LABELS = {"running": "运行中", "success": "已完成", "failed": "失败", "cancelled": "已取消", "interrupted": "中断待检查"}


def button(text: str, callback, primary: bool = False) -> QPushButton:
    widget = QPushButton(text)
    widget.setProperty("primary", primary)
    widget.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
    widget.clicked.connect(callback)
    return widget


def table(headers: list[str]) -> QTableWidget:
    widget = ReadableTable(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setAlternatingRowColors(True)
    widget.verticalHeader().hide()
    widget.verticalHeader().setDefaultSectionSize(42)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    widget.setShowGrid(False)
    return widget


def text_view() -> QTextEdit:
    view = QTextEdit()
    view.setReadOnly(True)
    return view


class ResourcePage(QWidget):
    def __init__(self, window: MainWindow, title: str, subtitle: str, kinds: list[str]) -> None:
        super().__init__()
        self.window, self.kinds = window, kinds
        self.rows: list[Resource] = []
        self.compact = len(kinds) == 1
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        heading = QLabel(title)
        heading.setObjectName("Title")
        layout.addWidget(heading)
        description = QLabel(subtitle)
        description.setObjectName("Subtitle")
        description.setWordWrap(True)
        layout.addWidget(description)
        toolbar = QHBoxLayout()
        self.add = button("＋ 添加工作 Agent" if kinds == ["agent"] else "＋ 添加" + title, self.add_resource, True)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名称或目录")
        self.search.textChanged.connect(self.refresh)
        toolbar.addWidget(self.add)
        self.selector = QComboBox(self)
        self.selector.setMinimumContentsLength(16)
        self.selector.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.selector.currentIndexChanged.connect(self.select_instance)
        toolbar.addWidget(self.selector if self.compact else self.search, 1)
        if self.compact:
            self.search.hide()
        toolbar.addWidget(button("编辑", lambda: window.edit_resource(self.selected())))
        layout.addLayout(toolbar)
        actions = FlowLayout()
        self.observe_button = button("检查状态", lambda: self.dispatch("observe"))
        self.backup_button = button("立即备份", lambda: self.dispatch("backup"), True)
        self.verify_button = button("校验备份", lambda: self.dispatch("verify"))
        self.restore_button = button("恢复备份", lambda: self.dispatch("restore"))
        self.more_button = button("登记管理", self.more)
        self.records_button = button("浏览本地记录", lambda: self.dispatch("records"))
        if kinds == ["agent"]:
            actions.addWidget(self.records_button)
        for widget in [self.observe_button, self.backup_button, self.verify_button, self.restore_button]:
            actions.addWidget(widget)
        self.library_button = button("会话与技能", lambda: self.dispatch("library"))
        self.logs_button = button("网关日志", lambda: self.dispatch("logs"))
        self.search_records_button = button("搜索记录", lambda: self.window.search_records(self.selected()))
        if kinds == ["hermes_local"]:
            actions.addWidget(self.library_button)
        elif kinds == ["hermes_server"]:
            actions.addWidget(self.logs_button)
            actions.addWidget(button("重启共享网关", lambda: self.dispatch("restart")))
        elif kinds == ["agent"]:
            actions.addWidget(self.search_records_button)
            self.usage_button = button("Token 用量", self.show_usage)
            actions.addWidget(self.usage_button)
            actions.addWidget(button("试一次恢复", lambda: self.window.rehearse_backup(self.selected()) if self.selected() else None))
            actions.addWidget(button("打开资料目录", lambda: self.dispatch("open")))
        if kinds == ["hermes_local"]:
            actions.addWidget(button("备份提交", lambda: self.dispatch("versions")))
            actions.addWidget(button("打开 Hermes 目录", lambda: self.dispatch("open")))
        elif kinds == ["hermes_server"]:
            actions.addWidget(button("查看服务器 Profiles", self.inspect_profiles))
        actions.addWidget(self.more_button)
        layout.addLayout(actions)
        self.metrics = []
        if self.compact:
            metrics = QHBoxLayout()
            for label in ("最近备份", "恢复检查", "运行 / 连接", "资料范围"):
                panel, value, note = card(label, "等待检查", "")
                note.hide()
                value.setStyleSheet("font-size: 17px; font-weight: 600;")
                value.setWordWrap(True)
                metrics.addWidget(panel, 1)
                self.metrics.append(value)
            layout.addLayout(metrics)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.resources_table = table(["名称", "类型", "位置", "状态"])
        self.resources_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.resources_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.resources_table.setColumnWidth(0, 200)
        self.resources_table.setColumnHidden(1, len(kinds) == 1)
        self.resources_table.setMinimumWidth(340)
        self.resources_table.itemSelectionChanged.connect(self.selection_changed)
        self.resources_table.doubleClicked.connect(lambda index: self.show_paths())
        if self.compact:
            self.resources_table.setParent(self)
            self.resources_table.hide()
        else:
            split.addWidget(self.resources_table)
        tabs = QTabWidget()
        self.details = text_view()
        self.technical_details = text_view()
        self.details.setMinimumWidth(240)
        tabs.addTab(self.details, "概览")
        self.tabs = tabs
        self.paths_page = QWidget()
        self.paths_layout = QVBoxLayout(self.paths_page)
        self.paths_layout.setContentsMargins(8, 8, 8, 8)
        paths_scroll = QScrollArea()
        paths_scroll.setWidgetResizable(True)
        paths_scroll.setWidget(self.paths_page)
        tabs.addTab(paths_scroll, "目录与操作")
        self.recent_details = text_view()
        tabs.addTab(self.recent_details, "最近操作")
        tabs.addTab(self.technical_details, "配置与诊断")
        split.addWidget(tabs)
        split.setSizes([650, 360])
        layout.addWidget(split, 1)
        self.empty = QLabel("还没有添加。先选择项目文件夹；工作 Agent 还可以添加保存聊天和配置的记录目录。")
        self.empty.setWordWrap(True)
        self.empty.setObjectName("Subtitle")
        layout.addWidget(self.empty)
        self.selection_changed()

    def add_resource(self) -> None:
        if len(self.kinds) == 1:
            self.window.add_resource(self.kinds[0])
        else:
            menu = QMenu(self)
            for kind in self.kinds:
                menu.addAction(KINDS[kind], lambda kind=kind: self.window.add_resource(kind))
            menu.exec(self.add.mapToGlobal(self.add.rect().bottomLeft()))

    def selected(self) -> Resource | None:
        row = self.resources_table.currentRow()
        return self.rows[row] if 0 <= row < len(self.rows) else None

    def select_instance(self, row: int) -> None:
        if 0 <= row < len(self.rows):
            self.resources_table.selectRow(row)

    def inspect_profiles(self) -> None:
        resource = self.selected()
        if resource:
            from .library import TextReportDialog
            self.window.submit(resource, "读取服务器 Profiles", lambda context: self.window.service.action(resource, "profiles", context),
                               lambda report: TextReportDialog(self.window, "服务器 Profiles", {"text": readable_report(report), "note": "每个 Profile 独立保存会话和配置。编辑登记时核对运行目录与备份仓库。"}).exec())

    def show_usage(self) -> None:
        resource = self.selected()
        if resource:
            from ..token_usage import read_usage
            from .library import TextReportDialog
            self.window.submit(resource, resource.name + " · 读取 Token 用量", lambda context: read_usage(resource, context),
                               lambda report: TextReportDialog(self.window, "本地 Token 用量", {"text": readable_report(report), "note": "手动读取本地用量字段；不估算费用、不启动后台监控。"}).exec())

    def refresh(self) -> None:
        selected = self.selected()
        query = self.search.text().strip().casefold()
        self.rows = [r for r in self.window.store.resources() if r.kind in self.kinds and query in (r.name + json.dumps(r.options, ensure_ascii=False)).casefold()]
        self.resources_table.blockSignals(True)
        self.resources_table.setRowCount(len(self.rows))
        for row, resource in enumerate(self.rows):
            location = str(resource.options.get("host") or resource.options.get("home") or resource.options.get("path") or next(iter(resource.options.get("record_paths", [])), "未配置"))
            observation = self.window.observations.get(resource.id, {})
            state = "任务运行中" if self.window.is_busy(resource.id) else str(observation.get("state") or self.window.resource_states.get(resource.id, "等待检查"))
            for column, value in enumerate([resource.name, KINDS[resource.kind], location, state]):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.resources_table.setItem(row, column, item)
        row = next((i for i, r in enumerate(self.rows) if selected and r.id == selected.id), 0)
        if self.rows:
            self.resources_table.selectRow(row)
        self.resources_table.blockSignals(False)
        self.selector.blockSignals(True)
        self.selector.clear()
        for resource in self.rows:
            self.selector.addItem(resource.name, resource.id)
        self.selector.setCurrentIndex(row if self.rows else -1)
        self.selector.blockSignals(False)
        self.empty.setVisible(not self.rows)
        self.selection_changed()

    def selection_changed(self) -> None:
        resource = self.selected()
        if resource:
            self.selector.blockSignals(True)
            self.selector.setCurrentIndex(self.resources_table.currentRow())
            self.selector.blockSignals(False)
        enabled = bool(resource and not self.window.is_busy(resource.id))
        for widget in [self.observe_button, self.backup_button, self.verify_button, self.restore_button, self.more_button,
                       self.library_button, self.logs_button, self.search_records_button]:
            widget.setEnabled(enabled)
        self.records_button.setEnabled(enabled)
        if not resource:
            self.details.setPlainText("选择一个资源，查看状态和可用操作。")
            self.technical_details.clear()
            self.recent_details.clear()
            self.populate_paths(None)
            return
        observation = self.window.observations.get(resource.id)
        capabilities = self.window.service.registry.get(resource).capabilities
        if not resource.options.get("manage_git"):
            capabilities = capabilities - {"git_pull"}
        payload = {"名称": resource.name, "类型": KINDS[resource.kind], "可用操作": "、".join(ACTION_LABELS.get(action, action) for action in sorted(capabilities) if action != "open_vault" or resource.kind == "vault")}
        if resource.kind == "agent":
            payload["使用的 Agent"] = resource.options.get("engine", "其他 Agent")
            payload["会备份哪些资料"] = [{"资料": item["label"], "文件夹": item["path"]} for item in sources_for(resource)]
            payload["换电脑怎么做"] = "立即备份 → 复制 .amb 文件和独立保存的口令 → 新电脑打开“备份与换电脑” → 从备份文件恢复。"
        if observation is not None:
            payload["最近检查结果"] = observation
            payload["提示"] = "最近结果仅代表执行时观测；点击检查状态获取新结果。"
        else:
            payload["提示"] = "尚未检查。配置已保存到本机；不会自动连接服务器。"
        self.technical_details.setPlainText(readable_report(payload))
        health = backup_health(self.window.store, resource, self.window.service.backup_root())
        if self.metrics:
            counts = (observation or {}).get("counts", {})
            values = [health["state"], "已演练" if health.get("rehearsed") else "已校验" if health["state"] == "已校验" else "尚待验证",
                      str((observation or {}).get("state") or ("已连接" if (observation or {}).get("connected") else "等待检查")),
                      f"{counts.get('sessions', '—')} 会话 · {counts.get('skills', '—')} 技能" if resource.kind == "hermes_local" else resource.options.get("profile_name", "独立资料目录")]
            for value, label in zip(values, self.metrics):
                label.setText(str(value))
        summary = f"<h2>{html.escape(resource.name)}</h2><p style='color:#7a849c'>{KINDS[resource.kind]}</p><hr><h3>资料保护</h3><p>{health['state']} · {html.escape(readable_time(health.get('created_at') or '尚无备份'))}</p>"
        summary += "<p>恢复演练：" + ("已通过" if health.get("rehearsed") else "尚未执行") + "</p>"
        if resource.kind == "hermes_server":
            if resource.options.get("profile_name"):
                summary += "<h3>当前 Profile：" + html.escape(resource.options['profile_name']) + "</h3><p>" + html.escape(resource.options.get("profile_home", "")) + "</p><p>两个 Profile 共用网关和现有服务器备份。原生备份 / 恢复按整套服务器资料执行。</p>"
            summary += "<h3>服务器</h3><p>" + ("连接成功 · 网关 " + html.escape(readable_report(observation.get('service_state', '未知'))) if observation and observation.get('connected') else "点击检查状态，测试 SSH、网关和备份环境。") + "</p>"
            if observation:
                summary += f"<p>磁盘 {observation.get('disk_used_percent', '未知')}% · 内存 {observation.get('memory_used_percent', '未知')}%</p><p>备份环境：{'就绪' if observation.get('backup_ready') else '请查看配置与诊断'}</p>"
        elif resource.kind == "hermes_local":
            counts = (observation or {}).get("counts", {})
            summary += f"<h3>Hermes 快照</h3><p>{counts.get('sessions', '—')} 个会话 · {counts.get('messages', '—')} 条消息</p><p>{counts.get('skills', '—')} 个技能 · {counts.get('facts', '—')} 条记忆</p>"
        elif resource.kind == "agent":
            summary += "<h3>本地资料</h3><p>" + "<br>".join(html.escape(item["label"] + " · " + item["path"]) for item in sources_for(resource)) + "</p>"
            summary += "<p>运行状态：" + html.escape(str((observation or {}).get("state", "尚未检查"))) + "</p>"
        else:
            summary += "<h3>项目位置</h3><p>" + html.escape(str(resource.options.get("path", ""))) + "</p>"
        policy_note = "手动按需备份 · 不自动连接服务器" if resource.kind == "hermes_server" else "每天自动备份 · 管家打开时生效" if resource.options.get("automatic_backup") else "自动备份未开启 · 可在编辑中设置"
        summary += "<hr><p style='color:#7a849c'>" + policy_note + "</p>"
        self.details.setHtml(summary)
        tasks = self.window.store.tasks(limit=5, resource_id=resource.id)
        self.recent_details.setPlainText("\n\n".join(readable_time(task['started_at']) + " · " + STATE_LABELS.get(task['state'], task['state']) + "\n" + task['title'] for task in tasks) or "这项资料还没有操作记录。检查状态、备份或恢复后，会显示在这里。")
        self.populate_paths(resource)

    def show_paths(self) -> None:
        self.tabs.setCurrentIndex(1)

    def populate_paths(self, resource: Resource | None) -> None:
        while item := self.paths_layout.takeAt(0):
            if widget := item.widget():
                widget.deleteLater()
        if resource is None:
            self.paths_layout.addWidget(QLabel("选中资料后显示完整目录和快捷操作。"))
            return
        title = QLabel(resource.name)
        title.setWordWrap(True)
        title.setObjectName("SectionTitle")
        self.paths_layout.addWidget(title)
        if resource.kind == "agent":
            paths = [(item["label"], item["path"]) for item in sources_for(resource)]
        else:
            paths = [(label, str(resource.options.get(key, ""))) for key, label in
                     (("profile_home", "当前 Profile 资料"), ("home", "运行 / 原生备份根目录"), ("path", "项目目录"), ("backup_repo", "备份仓库"), ("workspace", "Workspace"), ("knowledge_repo", "知识库"))]
        for label, path in paths:
            if not path:
                continue
            panel = QFrame()
            panel.setObjectName("DirectoryCard")
            content = QVBoxLayout(panel)
            content.addWidget(QLabel(label))
            view = QTextEdit()
            view.setObjectName("DirectoryPath")
            view.setReadOnly(True)
            view.setPlainText(path)
            view.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
            view.setMinimumHeight(52)
            view.setMaximumHeight(90)
            view.setToolTip(path)
            content.addWidget(view)
            controls = QHBoxLayout()
            controls.addWidget(button("复制路径", lambda checked=False, path=path: QApplication.clipboard().setText(path)))
            if resource.kind != "hermes_server":
                controls.addWidget(button("打开目录", lambda checked=False, path=path: self.window.open_path(Path(path))))
            controls.addStretch()
            content.addLayout(controls)
            self.paths_layout.addWidget(panel)
        self.paths_layout.addWidget(button("查看全部操作记录", lambda: self.window.show_resource_activity(resource)))
        self.paths_layout.addStretch()

    def dispatch(self, action: str) -> None:
        self.window.perform(self.selected(), action)

    def more(self) -> None:
        resource = self.selected()
        if not resource:
            return
        capabilities = self.window.service.registry.get(resource).capabilities
        labels = {"start": "启动", "stop": "停止", "restart": "重启网关", "git_pull": "Git 拉取（仅快进）", "versions": "查看备份提交", "open": "打开目录", "open_vault": "在 Obsidian 打开"}
        menu = QMenu(self)
        if resource.kind in {"project", "vault"}:
            menu.addAction("试一次恢复（临时目录）", lambda: self.window.rehearse_backup(resource))
        menu.addSeparator()
        for action, label in labels.items():
            if resource.kind in {"hermes_local", "hermes_server", "agent"}:
                continue
            if action in {"start", "stop"} and resource.kind in {"hermes_local", "agent"}:
                continue
            if action in capabilities and (action != "open_vault" or resource.kind == "vault") and (action != "git_pull" or resource.options.get("manage_git")):
                menu.addAction(label, lambda action=action: self.dispatch(action))
        menu.addSeparator()
        menu.addAction("移除登记", lambda: self.window.remove_resource(resource))
        menu.exec(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))


class MainWindow(QMainWindow):
    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self.service = ApplicationService(store)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.jobs: dict[str, tuple[Worker, str, object]] = {}
        self.resource_states: dict[str, str] = {r.id: "最近已检测" for r in store.resources() if store.evidence("observe:" + r.id)}
        self.observations: dict[str, dict] = {r.id: store.evidence("observe:" + r.id) for r in store.resources() if store.evidence("observe:" + r.id)}
        self.automatic_retry: dict[str, float] = {}
        self.setWindowTitle(f"Agent 管家 · {__version__}")
        self.resize(1280, 820)
        self.setMinimumSize(960, 660)
        shell = QWidget()
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(210)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(18, 30, 18, 18)
        brand = QLabel("Agent 管家")
        brand.setObjectName("Brand")
        sidebar_layout.addWidget(brand)
        sub = QLabel("PERSONAL AGENT SPACE")
        sub.setObjectName("BrandSub")
        sidebar_layout.addWidget(sub)
        sidebar_layout.addSpacing(22)
        self.navigation = QListWidget()
        self.navigation.setObjectName("Navigation")
        self.navigation.setIconSize(QSize(22, 22))
        for index, label in enumerate(["工作台", "本地 Hermes", "服务器 Hermes", "本地项目", "其他 Agent", "技能与工具", "备份与迁移", "工作总结", "设置"]):
            self.navigation.addItem(QListWidgetItem(nav_icon(4 if index == 5 else index if index < 5 else index - 1), label))
        sidebar_layout.addWidget(self.navigation, 1)
        help_button = button("使用说明", self.show_guide)
        help_button.setObjectName("SidebarHelp")
        sidebar_layout.addWidget(help_button)
        version = QLabel(f"v{__version__}  ·  {'便携模式' if store.portable_root else '本机模式'}")
        version.setObjectName("BrandSub")
        sidebar_layout.addWidget(version)
        shell_layout.addWidget(sidebar)
        content = QWidget()
        content.setObjectName("Content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 26, 24, 18)
        self.stack = QStackedWidget()
        content_layout.addWidget(self.stack)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        content_layout.addWidget(self.progress)
        shell_layout.addWidget(content, 1)
        self.setCentralWidget(shell)
        self.build_overview()
        self.resource_pages: list[ResourcePage] = []
        for title, subtitle, kinds in [
            ("本地 Hermes", "管理运行目录和现有恢复仓库，执行备份、校验与完整恢复。", ["hermes_local"]),
            ("服务器 Hermes", "按需连接服务器，管理网关、服务器备份与空目录恢复。", ["hermes_server"]),
            ("本地项目", "管理代码、知识库与 Obsidian；加密备份工作文件和附件。", ["project", "vault"]),
            ("其他工作 Agent", "备份 Codex、WorkBuddy 等的项目文件和本地记录；换电脑时一起恢复。", ["agent"])]:
            page = ResourcePage(self, title, subtitle, kinds)
            self.resource_pages.append(page)
            if kinds == ["project", "vault"]:
                from .projects import ProjectPage
                self.project_page = ProjectPage(self, page)
                self.stack.addWidget(self.project_page)
            else:
                self.stack.addWidget(page)
        from .assets import AssetPage
        self.asset_page = AssetPage(self)
        self.stack.addWidget(self.asset_page)
        self.build_backups()
        self.build_tasks()
        self.build_settings()
        self.navigation.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.navigation.currentRowChanged.connect(self.page_changed)
        self.navigation.setCurrentRow(0)
        self.statusBar().showMessage("准备就绪 · 不会自动连接服务器或执行恢复")
        self.refresh_resources()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh_tasks)
        self.timer.start()
        self.schedule_timer = QTimer(self)
        self.schedule_timer.setInterval(60_000)
        self.schedule_timer.timeout.connect(self.run_automatic_backup)
        self.schedule_timer.start()

    def build_overview(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(18)
        title = QLabel("工作台")
        title.setObjectName("Title")
        layout.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("DashboardBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 8, 0)
        body_layout.setSpacing(18)
        hero = QFrame()
        hero.setObjectName("Hero")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(26, 22, 26, 22)
        words = QVBoxLayout()
        heading = QLabel("把工作留住，随时接着做")
        heading.setObjectName("HeroTitle")
        words.addWidget(heading)
        self.overview_info = QLabel("Hermes · 项目 · 工作 Agent")
        self.overview_info.setObjectName("HeroNote")
        self.overview_info.setWordWrap(True)
        words.addWidget(self.overview_info)
        hero_layout.addLayout(words, 1)
        hero_layout.addWidget(button("备份 Hermes", self.backup_main_hermes, True))
        body_layout.addWidget(hero)
        grid = QGridLayout()
        self.count_labels: list[QLabel] = []
        for index, name in enumerate(["本地 Hermes", "服务器", "项目与知识库", "其他 Agent"]):
            panel, value, note = card(name, "0", "已登记资源")
            self.count_labels.append(value)
            grid.addWidget(panel, 0, index)
        body_layout.addLayout(grid)
        actions = QHBoxLayout()
        actions.addWidget(button("检查全部状态", self.observe_all))
        actions.addWidget(button("查看最近备份", lambda: self.navigation.setCurrentRow(6)))
        actions.addWidget(button("换电脑恢复", lambda: self.navigation.setCurrentRow(6)))
        actions.addStretch()
        body_layout.addLayout(actions)
        row = QHBoxLayout()
        section = QLabel("资料保护")
        section.setObjectName("SectionTitle")
        row.addWidget(section)
        row.addStretch()
        self.health_hint = QLabel("")
        self.health_hint.setObjectName("Subtitle")
        row.addWidget(self.health_hint)
        body_layout.addLayout(row)
        self.health_table = table(["资料", "最近备份", "保护状态", "连接 / 运行状态"])
        self.health_table.setMinimumHeight(205)
        self.health_table.setMaximumHeight(340)
        self.health_table.doubleClicked.connect(self.open_dashboard_resource)
        body_layout.addWidget(self.health_table)
        panels = QHBoxLayout()
        self.pending_items = QListWidget()
        self.latest_backups = QListWidget()
        for title, view in [("需要你处理", self.pending_items), ("最近备份", self.latest_backups)]:
            panel = QFrame()
            panel.setObjectName("Card")
            pane = QVBoxLayout(panel)
            heading = QLabel(title)
            heading.setObjectName("SectionTitle")
            pane.addWidget(heading)
            view.setWordWrap(True)
            view.setObjectName("ActionList")
            view.setMinimumHeight(110)
            view.setMaximumHeight(140)
            view.itemDoubleClicked.connect(self.open_dashboard_item)
            pane.addWidget(view)
            panels.addWidget(panel, 1)
        body_layout.insertLayout(3, panels)
        body_layout.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self.stack.addWidget(page)

    def build_backups(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("备份与换电脑")
        title.setObjectName("Title")
        layout.addWidget(title)
        hint = QLabel("恢复已备份的项目、笔记、聊天文件、数据库和附件，并重新登记新路径。不会安装原应用或恢复云端独有记录；续聊与登录需在原应用核对。Hermes 原生恢复请到对应页面。")
        hint.setWordWrap(True)
        hint.setObjectName("Subtitle")
        layout.addWidget(hint)
        outer = layout
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 6, 0)
        bar = FlowLayout()
        bar.addWidget(button("刷新备份列表", self.refresh_backups))
        bar.addWidget(button("恢复选中备份…", self.restore_selected_backup, True))
        bar.addWidget(button("开始换电脑检查", self.import_backup))
        bar.addWidget(button("恢复演练", self.rehearse_selected_backup))
        bar.addWidget(button("打开备份目录", lambda: self.open_path(self.service.backup_root())))
        bar.addWidget(button("能恢复什么", self.show_restore_scope))
        layout.addLayout(bar)
        from .migration import MigrationChecklist
        self.migration_checklist = MigrationChecklist()
        self.migration_task_ids = set()
        layout.addWidget(self.migration_checklist)
        repository_bar = QHBoxLayout()
        self.agent_repository_label = QLabel()
        self.agent_repository_label.setWordWrap(True)
        self.agent_repository_label.setObjectName("Subtitle")
        self.refresh_agent_repository()
        repository_bar.addWidget(self.agent_repository_label, 1)
        repository_bar.addWidget(button("选择仓库", self.choose_agent_repository))
        repository_bar.addWidget(button("整理 Agent 备份", self.organize_agent_repository))
        layout.addLayout(repository_bar)
        self.backup_table = table(["资源", "时间", "文件数", "大小", "校验状态"])
        self.backup_table.setMinimumHeight(180)
        layout.addWidget(self.backup_table)
        self.backup_rows: list[tuple[Resource, dict]] = []
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.stack.addWidget(page)

    def show_restore_scope(self) -> None:
        from .help import guide_path
        from .library import TextReportDialog
        path = guide_path().with_name("RESTORE_SCOPE.md")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = "说明文件缺失，请重新解压完整运行包。"
        TextReportDialog(self, "换电脑恢复 · 范围与验证", {"text": text, "note": "先恢复资料，再在原应用核对路径与登录。"}).exec()

    def refresh_agent_repository(self) -> None:
        path = self.store.setting("agent_backup_repository", "")
        self.agent_repository_label.setText("Agent 备份仓库：" + (path or "未选择（只存加密备份副本）"))

    def choose_agent_repository(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择专用 Agent 备份仓库或空文件夹", self.store.setting("agent_backup_repository", str(self.service.backup_root().parent)))
        if path:
            self.store.set_setting("agent_backup_repository", path)
            self.refresh_agent_repository()

    def organize_agent_repository(self) -> None:
        path = self.store.setting("agent_backup_repository", "")
        if not path:
            QMessageBox.information(self, "先选择仓库", "请选择专用 Agent 备份仓库，或一个空文件夹。myself 和项目文件夹不能选在这里。")
            return
        def completed(report):
            QMessageBox.information(self, "Agent 备份已整理", f"新增 {report['copied']} 份，已有 {report['existing']} 份，跳过 {report['skipped']} 份。\n" + report["note"])
        self.submit(None, "整理 Agent 备份仓库", lambda context: self.service.organize_agent_backups(Path(path), context), completed)

    def build_tasks(self) -> None:
        page = QWidget()
        outer = QVBoxLayout(page)
        title = QLabel("工作总结与操作记录")
        title.setObjectName("Title")
        outer.addWidget(title)
        self.activity_tabs = QTabWidget()
        summaries = QWidget()
        summary_layout = QVBoxLayout(summaries)
        note = QLabel("记下今天做了什么、遇到的问题和下一步。项目的具体日志在“本地项目”里写；这里保存你的总体总结。")
        note.setWordWrap(True)
        summary_layout.addWidget(note)
        self.summary_input = QTextEdit()
        self.summary_input.setPlaceholderText("完成了什么？下一步准备做什么？")
        self.summary_input.setMaximumHeight(150)
        summary_layout.addWidget(self.summary_input)
        summary_layout.addWidget(button("保存工作总结", self.save_work_summary), alignment=Qt.AlignmentFlag.AlignLeft)
        self.summary_history = text_view()
        path = self.store.root / "work-summary.md"
        if path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
            self.summary_history.setMarkdown(path.read_text(encoding="utf-8"))
        else:
            self.summary_history.setPlainText("还没有工作总结。这里不会自动复制 Agent 对话。")
        summary_layout.addWidget(self.summary_history, 1)
        self.activity_tabs.addTab(summaries, "我的工作总结")
        operations = QWidget()
        layout = QVBoxLayout(operations)
        hint = QLabel("任务结果与脱敏日志保存在这台电脑。取消会在安全检查点生效；恢复切换期间等待工具完成。")
        hint.setWordWrap(True)
        hint.setObjectName("Subtitle")
        layout.addWidget(hint)
        bar = QHBoxLayout()
        self.task_filter = QComboBox()
        self.task_filter.setMaximumWidth(300)
        self.task_filter.currentIndexChanged.connect(self.refresh_tasks)
        bar.addWidget(self.task_filter)
        bar.addWidget(button("刷新", self.refresh_tasks))
        bar.addWidget(button("请求取消选中任务", self.cancel_task))
        bar.addStretch()
        layout.addLayout(bar)
        split = QSplitter(Qt.Orientation.Vertical)
        self.task_table = table(["任务", "状态", "开始时间"])
        self.task_table.itemSelectionChanged.connect(self.task_details)
        split.addWidget(self.task_table)
        self.task_detail = text_view()
        split.addWidget(self.task_detail)
        split.setSizes([330, 220])
        layout.addWidget(split, 1)
        self.task_rows: list[dict] = []
        self.activity_tabs.addTab(operations, "操作记录 / 排查失败")
        outer.addWidget(self.activity_tabs, 1)
        self.stack.addWidget(page)

    def save_work_summary(self) -> None:
        from ..work_summaries import append_summary
        try:
            text = append_summary(self.store.root, self.summary_input.toPlainText())
            self.summary_history.setMarkdown(text)
            self.summary_input.clear()
            self.statusBar().showMessage("工作总结已保存，随管家资料一起带走")
        except (OSError, UserError) as exc:
            QMessageBox.warning(self, "总结未保存", str(exc))

    def build_settings(self) -> None:
        from .settings import build_settings
        build_settings(self)

    def toggle_settings_advanced(self, expanded: bool) -> None:
        self.settings_advanced.setVisible(expanded)
        self.settings_advanced_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)

    def show_guide(self) -> None:
        from .help import GuideDialog
        GuideDialog(self).exec()

    def refresh_vault_status(self) -> None:
        if not hasattr(self, "vault_status"):
            return
        secrets = self.service.secrets
        exists = secrets.vault and secrets.vault.exists()
        self.vault_status.setText("密码记忆：" + ("已解锁" if secrets.unlocked else "已锁定" if exists else "未设置，备份时手动输入即可"))
        self.vault_unlock_button.setText("输入总密码解锁" if exists else "开启记住密码")

    def is_busy(self, identity: str) -> bool:
        return any(resource_id == identity for _, resource_id, _ in self.jobs.values())

    def backup_main_hermes(self) -> None:
        resource = next((r for r in self.store.resources() if r.kind == "hermes_local"), None)
        if resource:
            self.perform(resource, "backup")
        else:
            self.add_resource("hermes_local")

    def observe_all(self) -> None:
        for resource in self.store.resources():
            if not self.is_busy(resource.id):
                self.perform(resource, "observe")

    def refresh_dashboard(self) -> None:
        if not hasattr(self, "health_table"):
            return
        priority = {"hermes_local": 0, "hermes_server": 1, "agent": 2, "project": 3, "vault": 4}
        self.dashboard_resources = sorted(self.store.resources(), key=lambda resource: priority[resource.kind])
        self.health_table.setRowCount(len(self.dashboard_resources))
        attention = 0
        for row, resource in enumerate(self.dashboard_resources):
            health = backup_health(self.store, resource, self.service.backup_root())
            if health["state"] not in {"已校验", "已演练"}:
                attention += 1
            observation = self.observations.get(resource.id) or self.store.evidence("observe:" + resource.id)
            state = "尚未检测"
            if self.is_busy(resource.id):
                state = "任务进行中"
            elif resource.kind == "hermes_server" and observation:
                state = "网关正常" if observation.get("connected") and observation.get("service_state") == "active" else "请检查连接 / 网关"
            elif resource.kind == "agent" and observation:
                state = "目录需重选" if not all(item.get("exists") for item in observation.get("components", [])) else "运行中" if observation.get("external_process_count") or observation.get("pid") else "未发现运行进程"
            elif observation.get("error"):
                state = "请检查目录 / 环境"
            elif observation:
                state = "已检测"
            observed_age = age_hours(observation.get("observed_at"))
            if observed_age is not None and observed_age > 1:
                state += "（历史）"
            values = [resource.name, readable_time(health.get("created_at") or "—"), health["state"], state]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value + ("\n最近检测：" + readable_time(observation["observed_at"]) if column == 3 and observation.get("observed_at") else ""))
                if column == 2:
                    item.setForeground(QColor("#39937b" if health["state"] in {"已校验", "已演练"} else "#b58136"))
                self.health_table.setItem(row, column, item)
        self.health_hint.setText(f"{attention} 项待备份或验证 · 双击查看")
        self.overview_info.setText(f"{len(self.dashboard_resources)} 项资料已登记 · {'便携模式，随 U 盘带走' if self.store.portable_root else '所有参数保存在本机'}")
        self.pending_items.clear()
        self.latest_backups.clear()
        recent = []
        for resource in self.dashboard_resources:
            health = backup_health(self.store, resource, self.service.backup_root())
            tasks = self.store.tasks(limit=1, resource_id=resource.id)
            failed = bool(tasks and tasks[0]["state"] in {"failed", "interrupted"})
            reason = ""
            if failed:
                reason = "最近操作未完成，查看原因"
            elif health["state"] not in {"已校验", "已演练"}:
                reason = {"未备份": "还没有备份", "已过期": "备份较旧，有变化就再备份", "待校验": "备份需要校验"}.get(health["state"], "检查备份是否可用")
            if reason:
                item = QListWidgetItem(resource.name + "\n" + reason)
                item.setData(Qt.ItemDataRole.UserRole, (resource.id, failed))
                self.pending_items.addItem(item)
            if health.get("created_at"):
                recent.append((str(health["created_at"]), resource, health["state"]))
        for timestamp, resource, state in sorted(recent, key=lambda entry: entry[0], reverse=True)[:5]:
            item = QListWidgetItem(resource.name + "\n" + readable_time(timestamp) + " · " + state)
            item.setData(Qt.ItemDataRole.UserRole, (resource.id, "backup"))
            self.latest_backups.addItem(item)
        if not self.pending_items.count():
            self.pending_items.addItem("当前没有待处理的备份或失败操作。")
        if not self.latest_backups.count():
            self.latest_backups.addItem("还没有备份。选中资料后点击“立即备份”。")

    def open_dashboard_item(self, item) -> None:
        target = item.data(Qt.ItemDataRole.UserRole)
        if not target:
            return
        resource = next((r for r in self.dashboard_resources if r.id == target[0]), None)
        if resource:
            if target[1] == "backup":
                self.navigation.setCurrentRow(6)
                row = next((index for index, (owner, _) in enumerate(self.backup_rows) if owner.id == resource.id), None)
                if row is not None:
                    self.backup_table.selectRow(row)
            elif target[1]:
                self.show_resource_activity(resource)
            else:
                self.open_dashboard_resource(self.health_table.model().index(self.dashboard_resources.index(resource), 0))

    def open_dashboard_resource(self, index) -> None:
        resource = self.dashboard_resources[index.row()]
        page_index = {"hermes_local": 1, "hermes_server": 2, "project": 3, "vault": 3, "agent": 4}[resource.kind]
        self.navigation.setCurrentRow(page_index)
        if page_index == 3:
            self.project_page.show_backup_resource(resource)
        page = self.resource_pages[page_index - 1]
        row = next((i for i, item in enumerate(page.rows) if item.id == resource.id), None)
        if row is not None:
            page.resources_table.selectRow(row)

    def run_automatic_backup(self) -> None:
        if self.jobs:
            return
        for resource in self.store.resources():
            if is_due(self.store, resource) and time.monotonic() >= self.automatic_retry.get(resource.id, 0):
                self.automatic_retry[resource.id] = time.monotonic() + 300
                self.submit(resource, resource.name + " · 自动备份", lambda context, resource=resource: self.service.automatic_backup(resource, context))
                break

    def unlock_vault(self) -> None:
        if self.jobs:
            QMessageBox.information(self, "任务运行中", "请在任务结束后更改口令库状态。")
            return
        exists = self.service.secrets.vault and self.service.secrets.vault.exists()
        password, ok = QInputDialog.getText(self, "记住备份密码", "输入总密码，解锁已记住的备份密码" if exists else "设置一个总密码，保护保存的备份密码（至少 8 个字符，请单独记住）", QLineEdit.EchoMode.Password)
        if not ok or not password:
            return
        if self.service.secrets.vault and not self.service.secrets.vault.exists():
            confirm, ok = QInputDialog.getText(self, "确认总密码", "再输入一次刚才设置的总密码", QLineEdit.EchoMode.Password)
            if not ok or confirm != password:
                QMessageBox.warning(self, "未解锁", "两次主口令不一致。")
                return
        try:
            self.service.secrets.unlock(password)
            self.refresh_vault_status()
            self.statusBar().showMessage("便携口令库已解锁；关闭程序后自动锁定")
        except UserError as exc:
            QMessageBox.warning(self, "未解锁", str(exc))

    def lock_vault(self) -> None:
        if self.jobs:
            QMessageBox.information(self, "任务运行中", "请在任务结束后锁定口令库。")
            return
        self.service.secrets.lock()
        self.refresh_vault_status()
        self.statusBar().showMessage("便携口令库已锁定")

    def rehearse_selected_backup(self) -> None:
        row = self.backup_table.currentRow()
        if 0 <= row < len(self.backup_rows):
            resource, report = self.backup_rows[row]
            self.rehearse_backup(resource, report.get("archive", ""))

    def rehearse_backup(self, resource: Resource, source: str = "") -> None:
        if resource.kind in {"hermes_local", "hermes_server"}:
            QMessageBox.information(self, "Hermes 原生恢复", "Hermes 请先校验原生快照；服务器可通过恢复清单选择新的空目录演练。")
            return
        source = source or backup_health(self.store, resource, self.service.backup_root()).get("source", "")
        if not source:
            QMessageBox.information(self, "还没有备份", "先为这项资料创建备份，再执行恢复演练。")
            return
        supplied = self.password_input(False)
        if not supplied:
            return
        def done(report):
            self.refresh_backups()
            self.refresh_dashboard()
            QMessageBox.information(self, "恢复演练通过", f"实际恢复并校验了 {report['identical_files']} 个文件，原目录未覆盖。")
        self.submit(resource, resource.name + " · 恢复演练", lambda context: self.service.rehearse(resource, Path(source), self.resolve_password(resource, *supplied), context), done)

    def search_records(self, resource: Resource | None = None) -> None:
        if not isinstance(resource, Resource):
            resource = None
        from .search import SearchDialog
        SearchDialog(self, resource).exec()

    def refresh_resources(self) -> None:
        resources = self.store.resources()
        groups = [["hermes_local"], ["hermes_server"], ["project", "vault"], ["agent"]]
        for label, kinds in zip(self.count_labels, groups):
            label.setText(str(sum(resource.kind in kinds for resource in resources)))
        for page in self.resource_pages:
            page.refresh()
        self.project_page.refresh_myself()
        self.refresh_dashboard()

    def add_resource(self, kind: str) -> None:
        dialog = ResourceDialog(self, kind)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_resource:
            self.store.save_resource(dialog.result_resource)
            self.refresh_resources()

    def edit_resource(self, resource: Resource | None) -> None:
        if resource is None:
            return
        if self.is_busy(resource.id) or resource.id in self.service.running_owned_ids():
            QMessageBox.information(self, "资源正在使用", "请等待任务完成并停止本工具启动的进程后修改配置。")
            return
        dialog = ResourceDialog(self, resource.kind, resource)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_resource:
            self.store.save_resource(dialog.result_resource)
            self.refresh_resources()

    def remove_resource(self, resource: Resource | None) -> None:
        if resource is None or self.is_busy(resource.id):
            return
        if resource.id in self.service.running_owned_ids():
            QMessageBox.information(self, "进程仍在运行", "请先停止本工具启动的进程，再移除资源登记。")
            return
        if QMessageBox.question(self, "移除资源登记", f"移除 {resource.name} 的登记？磁盘资料和备份保留。") == QMessageBox.StandardButton.Yes:
            self.store.remove_resource(resource.id)
            self.refresh_resources()

    def submit(self, resource: Resource | None, title: str, operation, callback=None, persist_result: bool = True) -> str:
        if resource and self.is_busy(resource.id):
            QMessageBox.information(self, "任务运行中", "同一资源已有任务，请等待完成。")
            return ""
        identity = uuid4().hex
        resource_id = resource.id if resource else "system"
        self.store.start_task(identity, resource_id, title)
        worker = Worker(identity, self.store, operation, persist_result)
        worker.signals.finished.connect(self.task_finished)
        self.jobs[identity] = (worker, resource_id, callback)
        self.pool.start(worker)
        self.progress.show()
        self.statusBar().showMessage(title + " · 后台执行中")
        self.refresh_resources()
        self.refresh_tasks()
        return identity

    def task_finished(self, identity: str, result, state: str) -> None:
        entry = self.jobs.pop(identity, None)
        if not entry:
            return
        _, resource_id, callback = entry
        if identity in self.migration_task_ids:
            self.migration_task_ids.discard(identity)
            if state in {"failed", "cancelled"}:
                for index in (1, 2, 3, 4):
                    text = self.migration_checklist.table.item(index, 1).text()
                    if not text.startswith(("通过", "已恢复")):
                        self.migration_checklist.set_step(index, "未通过：" + str(result.get("error", STATE_LABELS.get(state, state))))
                        break
        observation = self.store.evidence("observe:" + resource_id)
        if observation:
            self.observations[resource_id] = observation
        self.resource_states[resource_id] = "最近任务完成" if state == "success" else STATE_LABELS.get(state, state)
        self.progress.setVisible(bool(self.jobs))
        self.refresh_resources()
        self.refresh_tasks()
        self.statusBar().showMessage(f"任务{STATE_LABELS.get(state, state)} · 详情见任务记录")
        if state == "success" and callback:
            try:
                callback(result)
            except (UserError, OSError, ValueError) as exc:
                QMessageBox.warning(self, "任务完成，后续处理未完成", "资料操作已结束，但登记或界面更新未完成。请核对操作结果与文件权限。（" + type(exc).__name__ + "）")
        elif state == "failed":
            QMessageBox.warning(self, "操作未完成", str(result.get("error", "请查看任务记录。")))
        self.refresh_dashboard()
        self.refresh_common_password()

    def password_input(self, creating: bool) -> tuple[str, bool] | None:
        dialog = PasswordDialog(self, creating)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.password.text(), dialog.remember.isChecked()

    def resolve_password(self, resource: Resource, supplied: str, remember: bool = False) -> str:
        password = supplied or self.service.common_password or self.service.secrets.get(resource.id + ":backup")
        if not password:
            raise UserError("请在设置中输入统一备份密码，或输入这份旧备份原来的密码。")
        if remember:
            self.service.common_password = password
        return password

    def refresh_common_password(self) -> None:
        if hasattr(self, "common_password_status"):
            self.common_password_status.setText("已输入 · 本次打开期间，所有新建 .amb 备份共用它" if self.service.common_password else "尚未输入 · 第一次备份时也可以设置")

    def set_common_password(self) -> None:
        supplied = self.password_input(True)
        if supplied and supplied[0]:
            self.service.common_password = supplied[0]
            self.refresh_common_password()

    def forget_common_password(self) -> None:
        self.service.common_password = ""
        self.refresh_common_password()

    def maintenance_tools(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("电脑维护")
        dialog.resize(600, 340)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("这些操作用于排查问题或迁移登记清单。完整资料恢复请到“备份与换电脑”。"))
        actions = FlowLayout()
        for label, action in [("检查电脑环境", self.diagnostics), ("导出登记清单", self.export_config), ("导入登记清单", self.import_config),
                              ("解锁旧版密码记忆", self.unlock_vault), ("打开管家资料", lambda: self.open_path(self.store.root))]:
            actions.addWidget(button(label, action))
        layout.addLayout(actions)
        self.settings_info = text_view()
        layout.addWidget(self.settings_info)
        dialog.exec()

    def perform(self, resource: Resource | None, action: str, archive_source: str = "") -> None:
        if resource is None:
            return
        if action == "open_vault":
            root = Path(resource.options.get("path", "")).expanduser()
            if not root.is_dir():
                QMessageBox.warning(self, "目录不存在", "请检查 Vault 目录配置。")
                return
            url = QUrl("obsidian://open")
            query = QUrlQuery()
            query.addQueryItem("vault", str(root.resolve()))
            url.setQuery(query)
            if not QDesktopServices.openUrl(url):
                QMessageBox.warning(self, "未能打开", "请确认这台电脑已安装 Obsidian 并登记了此 Vault。")
            return
        if action == "open":
            self.open_path(Path(resource.options.get("path") or resource.options.get("home") or next(iter(resource.options.get("record_paths", [])), "")))
            return
        if action == "records":
            from ..records import list_records
            self.submit(resource, resource.name + " · 查找本地记录", lambda context: list_records(resource, context),
                        lambda report: RecordsDialog(self, resource, report).exec())
            return
        if action == "library":
            from .library import HermesLibraryDialog
            self.submit(resource, resource.name + " · 查看会话与技能", lambda context: self.service.action(resource, "library", context),
                        lambda report: HermesLibraryDialog(self, resource, report).exec(), persist_result=False)
            return
        if action == "logs":
            from .library import TextReportDialog
            self.submit(resource, resource.name + " · 查看网关日志", lambda context: self.service.action(resource, "logs", context),
                        lambda report: TextReportDialog(self, resource.name + " · 网关日志", report).exec(), persist_result=False)
            return
        if action == "observe":
            def observed(report):
                self.observations[resource.id] = safe_result(report)
                self.observations[resource.id]["observed_at"] = now()
                self.resource_states[resource.id] = "已检查 · 详情见右侧"
                self.refresh_resources()
            self.submit(resource, resource.name + " · 检查状态", lambda context: self.service.observe(resource, context), observed)
            return
        if action == "backup":
            if resource.kind in {"hermes_local", "hermes_server"}:
                scope = "\n服务器现有原生工具备份整套默认目录和 Profiles；两个 Profile 共用此备份。" if resource.kind == "hermes_server" and resource.options.get("profile_name") else ""
                if QMessageBox.question(self, "创建备份", f"为 {resource.name} 创建备份？将沿用此资源的 Git 推送配置。" + scope) != QMessageBox.StandardButton.Yes:
                    return
                self.submit(resource, resource.name + " · 备份", lambda context: self.service.backup(resource, context))
            else:
                supplied = ("", False) if self.service.common_password else self.password_input(True)
                if supplied:
                    self.submit(resource, resource.name + " · 加密备份", lambda context: self.service.backup(resource, context, self.resolve_password(resource, *supplied)), self.backup_completed)
            return
        if action == "verify":
            if resource.kind in {"hermes_local", "hermes_server"}:
                self.submit(resource, resource.name + " · 校验", lambda context: self.service.action(resource, "verify", context))
            else:
                source = archive_source or QFileDialog.getOpenFileName(self, "选择加密备份", str(self.service.backup_root()), "Agent 备份 (*.amb)")[0]
                supplied = self.password_input(False) if source else None
                if source and supplied:
                    self.submit(resource, resource.name + " · 解密校验", lambda context: self.service.verify_backup(Path(source), self.resolve_password(resource, *supplied), context), lambda report: self.refresh_backups())
            return
        if action == "restore":
            self.begin_restore(resource, archive_source)
            return
        if action in {"start", "stop", "restart", "git_pull"}:
            prompt = {"start": "启动", "stop": "停止", "restart": "重启网关", "git_pull": "拉取 Git 更新"}[action]
            message = f"对 {resource.name} 执行“{prompt}”？"
            if resource.kind == "hermes_server" and resource.options.get("profile_name"):
                message += "\n这是共享网关服务，会影响使用该服务的全部 Profile。"
            if action == "start" and resource.kind != "hermes_server":
                message += "\n\n程序：" + str(resource.options.get("executable") or "尚未配置") + "\n参数：" + str(safe_result(resource.options.get("arguments", [])))
            if QMessageBox.question(self, "确认操作", message) != QMessageBox.StandardButton.Yes:
                return
        self.submit(resource, resource.name + " · " + {"start": "启动", "stop": "停止", "restart": "重启", "git_pull": "Git 拉取", "versions": "备份提交"}.get(action, action), lambda context: self.service.action(resource, action, context))

    def begin_restore(self, resource: Resource, archive_source: str = "") -> None:
        if resource.kind in {"project", "vault", "agent"}:
            self.import_backup(archive_source, resource)
            return
        source, target, supplied = "", "", ("", False)
        if resource.kind == "hermes_server":
            target, ok = QInputDialog.getText(self, "服务器恢复目标", "服务器上的新目录或空目录（绝对路径）", text="/home/hermes/hermes-restored")
            if not ok or not target.strip():
                return
        elif resource.kind != "hermes_local":
            candidates = archives.list_archives(self.service.backup_root(), resource.id)
            source = archive_source or (candidates[0]["archive"] if candidates else "")
            root = Path(resource.options["path"]).expanduser()
            dialog = ArchiveRestoreDialog(self, source, str(root.with_name(root.name + "-restored")))
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            source, target = dialog.source.text().strip(), dialog.target.text().strip()
            if not source or not target:
                QMessageBox.warning(self, "信息未填写", "请选择备份文件并填写恢复目标。")
                return
            supplied = self.password_input(False)
            if supplied is None:
                return
        def preview(context):
            password = self.resolve_password(resource, *supplied) if resource.kind not in {"hermes_local", "hermes_server"} else ""
            return self.service.plan_restore(resource, context, source=source, target=target, password=password)
        def confirm(plan: RestorePlan):
            if PlanDialog(self, resource, plan).exec() == QDialog.DialogCode.Accepted:
                def restore(context):
                    password = self.resolve_password(resource, *supplied) if resource.kind not in {"hermes_local", "hermes_server"} else ""
                    return self.service.restore(resource, plan, context, password)
                self.submit(resource, resource.name + " · 执行恢复", restore)
        self.submit(resource, resource.name + " · 恢复预览", preview, confirm)

    def backup_completed(self, report: dict) -> None:
        self.refresh_backups()
        QMessageBox.information(self, "备份已保存", f"已保存 {report['file_count']} 个文件。\n\n备份文件：\n{report['archive']}\n\n换电脑时复制这个 .amb 文件，并准备好备份口令；在新电脑点击“从备份文件恢复”。")

    def import_backup(self, archive_source: str = "", resource: Resource | None = None) -> None:
        source = archive_source or QFileDialog.getOpenFileName(self, "选择要恢复的备份文件", str(self.service.backup_root()), "加密资料备份 (*.amb)")[0]
        if not source:
            return
        self.navigation.setCurrentRow(6)
        checklist = self.migration_checklist
        checklist.reset()
        checklist.set_step(0, "已选择：" + Path(source).name)
        supplied = self.password_input(False)
        if supplied is None:
            checklist.set_step(1, "尚未输入密码，可重新开始检查")
            return
        password = supplied[0] or self.service.common_password
        if not password and resource:
            try:
                password = self.resolve_password(resource, "")
            except UserError as exc:
                QMessageBox.warning(self, "需要备份口令", str(exc))
                return
        if not password:
            checklist.set_step(1, "缺少创建备份时使用的密码")
            QMessageBox.warning(self, "需要备份口令", "换电脑恢复时请输入创建这份备份时使用的口令。")
            return
        def inspected(report):
            checklist.set_step(1, "通过 · 密码可以解锁这份备份")
            checklist.set_step(2, f"通过 · 已校验 {report['file_count']} 个文件")
            if report["kind"] not in {"project", "vault", "agent"}:
                QMessageBox.warning(self, "请选择对应恢复入口", "Hermes 原生备份请在 Hermes 页面恢复。")
                return
            identity = report["resource_id"]
            if resource and identity not in {resource.id, resource.options.get("source_resource_id")}:
                QMessageBox.warning(self, "备份不匹配", "这份备份属于其他资料。请在“备份与换电脑”中使用“从备份文件恢复”。")
                return
            original = resource or Resource(report["resource_name"], report["kind"], {}, identity)
            safe_title = "".join(char for char in report["resource_name"] if char not in '/\\:*?"<>|').strip(". ") or identity[:8]
            restore_root = self.store.portable_root / "restored" if self.store.portable_root else Path.home() / "Documents/Agent恢复"
            dialog = TransferDialog(self, report, str(restore_root / safe_title))
            if dialog.exec() != QDialog.DialogCode.Accepted:
                checklist.set_step(3, "尚未选定目标目录，可重新开始")
                return
            target = dialog.target.text().strip()
            folders = {key: edit.text().strip() for key, edit in dialog.folder_fields.items()}
            def preview(context):
                plan = self.service.plan_restore(original, context, source=source, target=target, password=password, folders=folders)
                if plan.token != report["archive_sha256"]:
                    raise UserError("备份文件在解锁后改变，请重新选择文件。")
                return plan
            def confirm(plan):
                checklist.set_step(3, "通过 · 新目录 / 空目录：" + plan.target)
                if PlanDialog(self, original, plan).exec() != QDialog.DialogCode.Accepted:
                    checklist.set_step(4, "已取消执行，原目录未修改")
                    return
                checklist.set_step(4, "正在恢复 · 请等待文件校验完成")
                def completed(result):
                    checklist.set_step(4, "已恢复并校验 · 资料登记完成")
                    new_resource = restored_resource(report, Path(plan.target), plan.summary["component_folders"])
                    if any(item.id == new_resource.id for item in self.store.resources()):
                        new_resource.options["source_resource_id"] = new_resource.id
                        new_resource.id = uuid4().hex
                        new_resource.name += "（恢复）"
                    self.store.save_resource(new_resource)
                    if supplied[1]:
                        self.service.common_password = password
                        self.refresh_common_password()
                    self.refresh_resources()
                    self.navigation.setCurrentRow(4 if new_resource.kind == "agent" else 3)
                    for page in self.resource_pages:
                        row = next((i for i, item in enumerate(page.rows) if item.id == new_resource.id), None)
                        if row is not None:
                            page.resources_table.selectRow(row)
                    QMessageBox.information(self, "恢复完成", "资料已恢复并在这台电脑登记。\n\n位置：" + plan.target + "\n\n工作 Agent 可点击“浏览本地记录”。在原应用继续工作前，请核对项目路径并按需重新登录。")
                self.migration_task_ids.add(self.submit(original, original.name + " · 换电脑恢复", lambda context: self.service.restore(original, plan, context, password), completed))
            self.migration_task_ids.add(self.submit(original, original.name + " · 恢复预览", preview, confirm))
        self.migration_task_ids.add(self.submit(resource, "解锁并检查备份内容", lambda context: archives.verify_archive(Path(source), password, context), inspected))

    def refresh_tasks(self) -> None:
        if not hasattr(self, "task_table"):
            return
        resource_id = self.task_filter.currentData()
        self.task_filter.blockSignals(True)
        self.task_filter.clear()
        self.task_filter.addItem("全部资源", None)
        for resource in self.store.resources():
            self.task_filter.addItem(resource.name, resource.id)
            self.task_filter.setItemData(self.task_filter.count() - 1, resource.name, Qt.ItemDataRole.ToolTipRole)
        self.task_filter.setCurrentIndex(max(0, self.task_filter.findData(resource_id)))
        self.task_filter.blockSignals(False)
        previous = self.task_table.currentRow()
        selected = self.task_rows[previous]["id"] if 0 <= previous < len(self.task_rows) else None
        self.task_rows = self.store.tasks(resource_id=self.task_filter.currentData())
        self.task_table.blockSignals(True)
        self.task_table.setRowCount(len(self.task_rows))
        for row, task in enumerate(self.task_rows):
            timestamp = datetime.fromisoformat(task["started_at"]).astimezone().strftime("%m-%d %H:%M")
            for column, value in enumerate([task["title"], STATE_LABELS.get(task["state"], task["state"]), timestamp]):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.task_table.setItem(row, column, item)
        if self.task_rows:
            self.task_table.selectRow(next((i for i, task in enumerate(self.task_rows) if task["id"] == selected), 0))
        self.task_table.blockSignals(False)
        self.task_details()

    def show_resource_activity(self, resource: Resource) -> None:
        self.navigation.setCurrentRow(7)
        self.activity_tabs.setCurrentIndex(1)
        self.task_filter.setCurrentIndex(self.task_filter.findData(resource.id))
        self.refresh_tasks()

    def task_details(self) -> None:
        row = self.task_table.currentRow()
        if 0 <= row < len(self.task_rows):
            task = self.task_rows[row]
            self.task_detail.setPlainText(task["log"] + "\n结果\n" + readable_report(json.loads(task["result"])))
        else:
            self.task_detail.clear()

    def cancel_task(self) -> None:
        row = self.task_table.currentRow()
        if 0 <= row < len(self.task_rows):
            identity = self.task_rows[row]["id"]
            if identity in self.jobs:
                self.jobs[identity][0].context.cancel.set()
                self.store.append_log(identity, "已请求取消；不可中断的写入步骤将等待完成。")
                self.refresh_tasks()

    def page_changed(self, index: int) -> None:
        if index == 5:
            self.asset_page.refresh()
        elif index == 6:
            self.refresh_backups()
        elif index == 7:
            self.refresh_tasks()

    def refresh_backups(self) -> None:
        if not hasattr(self, "backup_table"):
            return
        self.backup_rows = []
        for resource in self.store.resources():
            if resource.kind in {"project", "vault", "agent"}:
                self.backup_rows.extend((resource, report) for report in archives.list_archives(self.service.backup_root(), resource.id))
            elif resource.kind == "hermes_local":
                repo = resource.options.get("backup_repo", "")
                if repo and (Path(repo).expanduser() / "snapshot/snapshot.json").is_file():
                    self.backup_rows.append((resource, {"created_at": "当前 snapshot", "file_count": "—", "size": 0, "native": True}))
        self.backup_table.setRowCount(len(self.backup_rows))
        for row, (resource, report) in enumerate(self.backup_rows):
            if report.get("native"):
                status = backup_health(self.store, resource, self.service.backup_root())["state"]
            else:
                path = Path(report["archive"])
                evidence = self.store.evidence(str(path.resolve()))
                unchanged = all(evidence.get(key) == value for key, value in fingerprint(path).items())
                status = "已演练" if unchanged and evidence.get("rehearsed_at") else "已校验" if unchanged and evidence.get("verified_at") else "待校验"
            values = [resource.name, readable_time(report.get("created_at", "未知")), str(report.get("file_count", "—")),
                      "Hermes 原生" if report.get("native") else readable_size(report.get("size", 0)), status]
            for column, value in enumerate(values):
                self.backup_table.setItem(row, column, QTableWidgetItem(value))

    def restore_selected_backup(self) -> None:
        row = self.backup_table.currentRow()
        if 0 <= row < len(self.backup_rows):
            resource, report = self.backup_rows[row]
            self.perform(resource, "restore", report.get("archive", ""))

    def save_settings(self) -> None:
        value = self.backup_path.text().strip()
        if not value:
            QMessageBox.warning(self, "未保存", "请选择备份目录。")
            return
        if self.jobs:
            QMessageBox.information(self, "任务运行中", "请在任务完成后修改备份目录。")
            return
        self.store.set_setting("backup_root", str(Path(value).expanduser().resolve()))
        self.store.set_setting("backup_keep", self.backup_keep.value())
        self.statusBar().showMessage("设置已保存")

    def change_backup_location(self) -> None:
        if self.jobs:
            QMessageBox.information(self, "任务运行中", "请等待任务完成后再更改位置。")
            return
        directory = QFileDialog.getExistingDirectory(self, "以后创建的备份放在哪里", str(self.service.backup_root()))
        if directory:
            self.backup_path.setText(directory)
            self.save_settings()
            self.backup_location.setPlainText(str(self.service.backup_root()))
            self.refresh_backups()
            self.refresh_dashboard()

    def export_config(self) -> None:
        destination = QFileDialog.getSaveFileName(self, "导出资源配置", "agent-manager-resources.json", "JSON (*.json)")[0]
        if destination:
            try:
                self.store.export_config(Path(destination))
                self.statusBar().showMessage("资源配置已导出；凭据和任务记录未包含")
            except (OSError, UserError) as exc:
                QMessageBox.warning(self, "导出失败", str(exc))

    def import_config(self) -> None:
        source = QFileDialog.getOpenFileName(self, "导入资源配置", "", "JSON (*.json)")[0]
        if source:
            try:
                count = self.store.import_config(Path(source))
                self.refresh_resources()
                self.statusBar().showMessage(f"已合并 {count} 项新资源，请核对这台电脑的路径")
            except (OSError, ValueError, UserError) as exc:
                QMessageBox.warning(self, "导入失败", str(exc))

    def discover_workspace(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择 Hermes Workspace", str(Path.cwd()))
        if not selected:
            return
        root = Path(selected).resolve()
        def discover(context):
            manifest = root / "workspace.json"
            if not manifest.is_file() or manifest.stat().st_size > 1_000_000:
                raise UserError("此目录没有有效 workspace.json。")
            document = json.loads(manifest.read_text(encoding="utf-8"))
            candidates = []
            homes = [Path(os.environ["HERMES_HOME"]).expanduser()] if os.environ.get("HERMES_HOME") else []
            homes += [root.parent / "hermes", Path.home() / ".hermes"]
            home = next((path for path in homes if (path / "config.yaml").is_file()), Path.home() / ".hermes")
            for item in document.get("repositories", []):
                path = (root / item["path"]).resolve()
                if not path.is_relative_to(root) or not path.is_dir() or path.is_symlink():
                    continue
                if item.get("role") == "local-hermes-backup":
                    candidates.append(Resource("本地 Hermes", "hermes_local", {"home": str(home), "backup_repo": str(path), "workspace": str(root), "python": ResourceDialog.default_python(), "passphrase_file": str(home / ".hermes-backup-passphrase"), "push": True}))
                elif item.get("role") not in {"server-hermes-backup", "backup-archive", "agent-record-backups"}:
                    exclusions = [Path(entry["path"]).parts[0] for entry in document.get("repositories", []) if entry.get("path") not in {".", ""}] if path == root else []
                    candidates.append(Resource(item.get("id", path.name), "vault" if (path / ".obsidian").is_dir() else "project", {"path": str(path), "exclude_dirs": exclusions}))
            return {"candidates": [resource.to_dict() for resource in candidates]}
        def apply(report):
            candidates = [Resource.from_dict(item) for item in report["candidates"]]
            message = "发现以下资源：\n" + "\n".join(f"• {r.name}（{KINDS[r.kind]}）" for r in candidates) + "\n\n登记这些资源？服务器需要另外填写 SSH 连接。"
            if candidates and QMessageBox.question(self, "发现资源", message) == QMessageBox.StandardButton.Yes:
                existing = {(r.kind, r.options.get("path") or r.options.get("backup_repo")) for r in self.store.resources()}
                for resource in candidates:
                    key = (resource.kind, resource.options.get("path") or resource.options.get("backup_repo"))
                    if key not in existing:
                        self.store.save_resource(resource)
                self.refresh_resources()
        self.submit(None, "发现 Workspace 资源", discover, apply)

    def diagnostics(self) -> None:
        def show(report):
            self.settings_info.setPlainText("本机数据目录：" + report["data_directory"] + "\n\n可用工具：\n" + "\n".join(name + "：" + value for name, value in report["tools"].items()) + "\n\n普通项目和工作 Agent 备份无需单独配置 Python；Hermes 原生备份需要其工具环境。")
            self.navigation.setCurrentRow(8)
            self.settings_advanced_toggle.setChecked(True)
        self.submit(None, "检查运行依赖", lambda context: self.service.diagnostics(), show)

    def remove_secret(self) -> None:
        resources = [r for r in self.store.resources() if r.kind in {"project", "vault", "agent"}]
        if not resources:
            QMessageBox.information(self, "尚无资源", "登记项目或 Agent 后可管理对应备份口令。")
            return
        labels = [f"{r.name} · {r.id[:8]}" for r in resources]
        label, ok = QInputDialog.getItem(self, "移除系统凭据", "选择资源；加密备份保留，请确保口令另有保存", labels, editable=False)
        if ok and QMessageBox.question(self, "确认移除口令", "从这台电脑的系统凭据存储移除此口令？") == QMessageBox.StandardButton.Yes:
            resource = resources[labels.index(label)]
            def remove(context):
                self.service.secrets.remove(resource.id + ":backup")
                return {"removed": True}
            self.submit(resource, resource.name + " · 移除系统口令", remove)

    def open_path(self, path: Path) -> None:
        path = path.expanduser()
        if not path.is_dir():
            QMessageBox.warning(self, "目录不存在", "请检查目录配置。备份目录会在首次备份时创建。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def closeEvent(self, event) -> None:
        if self.jobs:
            QMessageBox.information(self, "任务仍在运行", "请等待任务完成，或在任务记录中请求取消。恢复切换期间不能关闭程序。")
            event.ignore()
            return
        if self.service.running_owned_ids():
            if QMessageBox.question(self, "Agent 仍在运行", "退出后启动的 Agent 将继续运行。重新打开程序不能接管这些进程。确认退出？") != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.timer.stop()
        self.schedule_timer.stop()
        self.project_page.stop_updates()
        self.service.common_password = ""
        self.service.secrets.lock()
        event.accept()
