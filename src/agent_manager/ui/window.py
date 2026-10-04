from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4
from datetime import datetime

from PySide6.QtCore import QThreadPool, QTimer, Qt, QUrl, QUrlQuery
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget,
    QMainWindow, QMenu, QMessageBox, QPushButton, QProgressBar, QSplitter, QStackedWidget,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget)

from .. import __version__, archives
from ..application import ApplicationService
from ..domain import KINDS, Resource, RestorePlan, UserError
from ..security import safe_result
from ..storage import Store
from .dialogs import ArchiveRestoreDialog, PasswordDialog, PlanDialog, ResourceDialog, RecordsDialog, TransferDialog, choose_path
from ..profiles import restored_resource, sources_for
from .tasks import Worker
from .presentation import ACTION_LABELS, readable_report, readable_time, readable_size

STATE_LABELS = {"running": "运行中", "success": "已完成", "failed": "失败", "cancelled": "已取消", "interrupted": "中断待检查"}


def button(text: str, callback, primary: bool = False) -> QPushButton:
    widget = QPushButton(text)
    widget.setProperty("primary", primary)
    widget.clicked.connect(callback)
    return widget


def table(headers: list[str]) -> QTableWidget:
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    widget.setAlternatingRowColors(True)
    widget.verticalHeader().hide()
    widget.verticalHeader().setDefaultSectionSize(42)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    widget.setWordWrap(False)
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
        toolbar.addWidget(self.search)
        toolbar.addWidget(button("编辑", lambda: window.edit_resource(self.selected())))
        toolbar.addWidget(button("移除登记", lambda: window.remove_resource(self.selected())))
        if kinds == ["hermes_local"]:
            toolbar.addWidget(button("从现有 Workspace 添加", window.discover_workspace))
        layout.addLayout(toolbar)
        actions = QHBoxLayout()
        self.observe_button = button("检查状态", lambda: self.dispatch("observe"))
        self.backup_button = button("立即备份", lambda: self.dispatch("backup"), True)
        self.verify_button = button("校验备份", lambda: self.dispatch("verify"))
        self.restore_button = button("恢复…", lambda: self.dispatch("restore"))
        self.more_button = button("更多操作", self.more)
        self.records_button = button("浏览本地记录", lambda: self.dispatch("records"))
        if kinds == ["agent"]:
            actions.addWidget(self.records_button)
        for widget in [self.observe_button, self.backup_button, self.verify_button, self.restore_button, self.more_button]:
            actions.addWidget(widget)
        actions.addStretch()
        layout.addLayout(actions)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.resources_table = table(["名称", "类型", "位置", "状态"])
        self.resources_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.resources_table.itemSelectionChanged.connect(self.selection_changed)
        self.resources_table.doubleClicked.connect(lambda index: self.dispatch("observe"))
        split.addWidget(self.resources_table)
        self.details = text_view()
        self.details.setMinimumWidth(240)
        split.addWidget(self.details)
        split.setSizes([700, 330])
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

    def refresh(self) -> None:
        selected = self.selected()
        query = self.search.text().strip().casefold()
        self.rows = [r for r in self.window.store.resources() if r.kind in self.kinds and query in (r.name + json.dumps(r.options, ensure_ascii=False)).casefold()]
        self.resources_table.blockSignals(True)
        self.resources_table.setRowCount(len(self.rows))
        for row, resource in enumerate(self.rows):
            location = str(resource.options.get("host") or resource.options.get("home") or resource.options.get("path") or next(iter(resource.options.get("record_paths", [])), "未配置"))
            state = "任务运行中" if self.window.is_busy(resource.id) else self.window.resource_states.get(resource.id, "等待检查")
            for column, value in enumerate([resource.name, KINDS[resource.kind], location, state]):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.resources_table.setItem(row, column, item)
        row = next((i for i, r in enumerate(self.rows) if selected and r.id == selected.id), 0)
        if self.rows:
            self.resources_table.selectRow(row)
        self.resources_table.blockSignals(False)
        self.empty.setVisible(not self.rows)
        self.selection_changed()

    def selection_changed(self) -> None:
        resource = self.selected()
        enabled = bool(resource and not self.window.is_busy(resource.id))
        for widget in [self.observe_button, self.backup_button, self.verify_button, self.restore_button, self.more_button]:
            widget.setEnabled(enabled)
        self.records_button.setEnabled(enabled)
        if not resource:
            self.details.setPlainText("选择一个资源，查看状态和可用操作。")
            return
        observation = self.window.observations.get(resource.id)
        capabilities = self.window.service.registry.get(resource).capabilities
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
        self.details.setPlainText(readable_report(payload))

    def dispatch(self, action: str) -> None:
        self.window.perform(self.selected(), action)

    def more(self) -> None:
        resource = self.selected()
        if not resource:
            return
        capabilities = self.window.service.registry.get(resource).capabilities
        labels = {"start": "启动", "stop": "停止", "restart": "重启网关", "git_pull": "Git 拉取（仅快进）", "versions": "查看备份提交", "open": "打开目录", "open_vault": "在 Obsidian 打开"}
        menu = QMenu(self)
        for action, label in labels.items():
            if action in capabilities and (action != "open_vault" or resource.kind == "vault"):
                menu.addAction(label, lambda action=action: self.dispatch(action))
        menu.exec(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))


class MainWindow(QMainWindow):
    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self.service = ApplicationService(store)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.jobs: dict[str, tuple[Worker, str, object]] = {}
        self.resource_states: dict[str, str] = {}
        self.observations: dict[str, dict] = {}
        self.setWindowTitle(f"Agent 管家 · {__version__}")
        self.resize(1280, 820)
        self.setMinimumSize(960, 660)
        shell = QWidget()
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(208)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(18, 30, 18, 18)
        brand = QLabel("Agent 管家")
        brand.setObjectName("Brand")
        sidebar_layout.addWidget(brand)
        sub = QLabel("Hermes 与工作 Agent")
        sub.setObjectName("BrandSub")
        sidebar_layout.addWidget(sub)
        sidebar_layout.addSpacing(22)
        self.navigation = QListWidget()
        self.navigation.setObjectName("Navigation")
        self.navigation.addItems(["开始使用", "本地 Hermes", "服务器 Hermes", "项目与 Obsidian", "其他工作 Agent", "备份与换电脑", "操作记录", "设置"])
        sidebar_layout.addWidget(self.navigation, 1)
        version = QLabel(f"v{__version__}  ·  本机配置")
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
            self.stack.addWidget(page)
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

    def build_overview(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("备份你的工作，换电脑接着用")
        title.setObjectName("Title")
        layout.addWidget(title)
        subtitle = QLabel("主要管理 Hermes，也保存 Codex、WorkBuddy 等工作 Agent 的项目和本地记录")
        subtitle.setWordWrap(True)
        subtitle.setObjectName("Subtitle")
        layout.addWidget(subtitle)
        layout.addSpacing(20)
        grid = QGridLayout()
        self.count_labels: list[QLabel] = []
        for index, name in enumerate(["本地 Hermes", "服务器", "项目与知识库", "Agent"]):
            card = QFrame()
            card.setObjectName("Card")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(18, 18, 18, 18)
            card_layout.addWidget(QLabel(name))
            value = QLabel("0")
            value.setObjectName("CardValue")
            self.count_labels.append(value)
            card_layout.addWidget(value)
            label = QLabel("已登记资源")
            label.setObjectName("Subtitle")
            card_layout.addWidget(label)
            grid.addWidget(card, 0, index)
        layout.addLayout(grid)
        layout.addSpacing(18)
        actions = QHBoxLayout()
        actions.addWidget(button("添加本地 Hermes", lambda: self.add_resource("hermes_local"), True))
        actions.addWidget(button("添加 Codex / WorkBuddy", lambda: self.add_resource("agent")))
        actions.addWidget(button("从备份文件恢复", self.import_backup))
        actions.addStretch()
        layout.addLayout(actions)
        layout.addSpacing(15)
        self.overview_info = text_view()
        layout.addWidget(self.overview_info, 1)
        self.stack.addWidget(page)

    def build_backups(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("备份与换电脑")
        title.setObjectName("Title")
        layout.addWidget(title)
        hint = QLabel("换电脑：复制 .amb 备份文件 → 点击“从备份文件恢复” → 输入备份口令 → 选择新文件夹。无需先导入资源配置。Hermes 原生恢复请到对应 Hermes 页面。")
        hint.setWordWrap(True)
        hint.setObjectName("Subtitle")
        layout.addWidget(hint)
        bar = QHBoxLayout()
        bar.addWidget(button("刷新备份列表", self.refresh_backups))
        bar.addWidget(button("恢复选中备份…", self.restore_selected_backup, True))
        bar.addWidget(button("从备份文件恢复", self.import_backup, True))
        bar.addWidget(button("打开备份目录", lambda: self.open_path(self.service.backup_root())))
        bar.addStretch()
        layout.addLayout(bar)
        self.backup_table = table(["资源", "时间", "文件数", "大小", "校验状态"])
        layout.addWidget(self.backup_table)
        self.backup_rows: list[tuple[Resource, dict]] = []
        self.stack.addWidget(page)

    def build_tasks(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("任务记录")
        title.setObjectName("Title")
        layout.addWidget(title)
        hint = QLabel("任务结果与脱敏日志保存在这台电脑。取消会在安全检查点生效；恢复切换期间等待工具完成。")
        hint.setWordWrap(True)
        hint.setObjectName("Subtitle")
        layout.addWidget(hint)
        bar = QHBoxLayout()
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
        self.stack.addWidget(page)

    def build_settings(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("设置")
        title.setObjectName("Title")
        layout.addWidget(title)
        hint = QLabel("路径可以在每台电脑重新配置。配置导入只合并新资源；凭据与任务记录不会随配置导出。")
        hint.setWordWrap(True)
        hint.setObjectName("Subtitle")
        layout.addWidget(hint)
        layout.addSpacing(20)
        row = QHBoxLayout()
        row.addWidget(QLabel("加密备份保存目录"))
        self.backup_path = QLineEdit(str(self.service.backup_root()))
        row.addWidget(self.backup_path, 1)
        row.addWidget(button("选择", lambda: choose_path(self.backup_path, self)))
        row.addWidget(button("保存", self.save_settings, True))
        layout.addLayout(row)
        tools = QHBoxLayout()
        tools.addWidget(button("导出资源配置", self.export_config))
        tools.addWidget(button("导入资源配置", self.import_config))
        tools.addWidget(button("检查运行环境", self.diagnostics))
        tools.addWidget(button("移除保存的备份口令", self.remove_secret))
        tools.addStretch()
        layout.addLayout(tools)
        self.settings_info = text_view()
        self.settings_info.setPlainText("管家的设置保存在：" + str(self.store.root) + "\n\n备份口令可以临时输入，也可以保存到这台电脑的安全凭据存储。换电脑请另存口令。\n\n项目与工作 Agent：一个 .amb 文件可以一起保存项目文件和本地记录，新电脑直接从备份文件恢复。\n\nHermes：使用原生备份仓库恢复；运行目录和口令文件需要在每台电脑重新选择。\n\n导出配置只导出登记清单，不能替代资料备份。")
        layout.addWidget(self.settings_info, 1)
        self.stack.addWidget(page)

    def is_busy(self, identity: str) -> bool:
        return any(resource_id == identity for _, resource_id, _ in self.jobs.values())

    def refresh_resources(self) -> None:
        resources = self.store.resources()
        groups = [["hermes_local"], ["hermes_server"], ["project", "vault"], ["agent"]]
        for label, kinds in zip(self.count_labels, groups):
            label.setText(str(sum(resource.kind in kinds for resource in resources)))
        for page in self.resource_pages:
            page.refresh()
        interrupted = sum(task["state"] == "interrupted" for task in self.store.tasks())
        self.overview_info.setPlainText(
            "第一次使用，按这三步：\n\n"
            "1. 添加要保存的资料\n   Hermes：选择运行目录与备份仓库。\n   Codex / WorkBuddy：选择项目文件夹，再添加保存聊天与配置的本地记录目录。\n   Obsidian：选择笔记库文件夹。\n\n"
            "2. 点击“立即备份”\n   先退出对应的 Agent；输入一个备份口令，保存好生成的 .amb 文件和口令。\n\n"
            "3. 换电脑，点击“从备份文件恢复”\n   选择 .amb 文件，输入口令，选择新文件夹。项目与记录将一起还原并登记。\n\n"
            "本地记录不等于云端记录。原应用可能需要重新登录、重新登记项目，或通过自己的导入功能读取记录。\n\n"
            f"当前有 {len(resources)} 项资源，{interrupted} 项中断任务待检查。\n"
            "备份生成、推送、文件校验和实际恢复是独立阶段。任务报告会保留实际结果。")

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

    def password_input(self, creating: bool) -> tuple[str, bool] | None:
        dialog = PasswordDialog(self, creating)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.password.text(), dialog.remember.isChecked()

    def resolve_password(self, resource: Resource, supplied: str, remember: bool = False) -> str:
        password = supplied or self.service.secrets.get(resource.id + ":backup")
        if not password:
            raise UserError("没有可用口令，请输入口令或保存到系统凭据存储。")
        if remember:
            self.service.secrets.save(resource.id + ":backup", password)
        return password

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
        if action == "observe":
            def observed(report):
                self.observations[resource.id] = safe_result(report)
                self.resource_states[resource.id] = "已检查 · 详情见右侧"
                self.refresh_resources()
            self.submit(resource, resource.name + " · 检查状态", lambda context: self.service.observe(resource, context), observed)
            return
        if action == "backup":
            if resource.kind in {"hermes_local", "hermes_server"}:
                if QMessageBox.question(self, "创建备份", f"为 {resource.name} 创建备份？将沿用此资源的 Git 推送配置。") != QMessageBox.StandardButton.Yes:
                    return
                self.submit(resource, resource.name + " · 备份", lambda context: self.service.backup(resource, context))
            else:
                supplied = self.password_input(True)
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
                    self.submit(resource, resource.name + " · 解密校验", lambda context: archives.verify_archive(Path(source), self.resolve_password(resource, *supplied), context))
            return
        if action == "restore":
            self.begin_restore(resource, archive_source)
            return
        if action in {"start", "stop", "restart", "git_pull"}:
            prompt = {"start": "启动", "stop": "停止", "restart": "重启网关", "git_pull": "拉取 Git 更新"}[action]
            message = f"对 {resource.name} 执行“{prompt}”？"
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
        supplied = self.password_input(False)
        if supplied is None:
            return
        password = supplied[0]
        if not password and resource:
            try:
                password = self.resolve_password(resource, "")
            except UserError as exc:
                QMessageBox.warning(self, "需要备份口令", str(exc))
                return
        if not password:
            QMessageBox.warning(self, "需要备份口令", "换电脑恢复时请输入创建这份备份时使用的口令。")
            return
        def inspected(report):
            if report["kind"] not in {"project", "vault", "agent"}:
                QMessageBox.warning(self, "请选择对应恢复入口", "Hermes 原生备份请在 Hermes 页面恢复。")
                return
            identity = report["resource_id"]
            if resource and identity not in {resource.id, resource.options.get("source_resource_id")}:
                QMessageBox.warning(self, "备份不匹配", "这份备份属于其他资料。请在“备份与换电脑”中使用“从备份文件恢复”。")
                return
            original = resource or Resource(report["resource_name"], report["kind"], {}, identity)
            safe_title = "".join(char for char in report["resource_name"] if char not in '/\\:*?"<>|').strip(". ") or identity[:8]
            dialog = TransferDialog(self, report, str(Path.home() / "Documents/Agent恢复" / safe_title))
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            target = dialog.target.text().strip()
            folders = {key: edit.text().strip() for key, edit in dialog.folder_fields.items()}
            def preview(context):
                plan = self.service.plan_restore(original, context, source=source, target=target, password=password, folders=folders)
                if plan.token != report["archive_sha256"]:
                    raise UserError("备份文件在解锁后改变，请重新选择文件。")
                return plan
            def confirm(plan):
                if PlanDialog(self, original, plan).exec() != QDialog.DialogCode.Accepted:
                    return
                def completed(result):
                    new_resource = restored_resource(report, Path(plan.target), plan.summary["component_folders"])
                    if any(item.id == new_resource.id for item in self.store.resources()):
                        new_resource.options["source_resource_id"] = new_resource.id
                        new_resource.id = uuid4().hex
                        new_resource.name += "（恢复）"
                    self.store.save_resource(new_resource)
                    if supplied[1]:
                        try:
                            self.service.secrets.save(new_resource.id + ":backup", password)
                        except UserError as exc:
                            QMessageBox.warning(self, "资料已恢复，口令未保存", str(exc))
                    self.refresh_resources()
                    self.navigation.setCurrentRow(4 if new_resource.kind == "agent" else 3)
                    for page in self.resource_pages:
                        row = next((i for i, item in enumerate(page.rows) if item.id == new_resource.id), None)
                        if row is not None:
                            page.resources_table.selectRow(row)
                    QMessageBox.information(self, "恢复完成", "资料已恢复并在这台电脑登记。\n\n位置：" + plan.target + "\n\n工作 Agent 可点击“浏览本地记录”。在原应用继续工作前，请核对项目路径并按需重新登录。")
                self.submit(original, original.name + " · 换电脑恢复", lambda context: self.service.restore(original, plan, context, password), completed)
            self.submit(original, original.name + " · 恢复预览", preview, confirm)
        self.submit(resource, "解锁并检查备份内容", lambda context: archives.verify_archive(Path(source), password, context), inspected)

    def refresh_tasks(self) -> None:
        if not hasattr(self, "task_table"):
            return
        previous = self.task_table.currentRow()
        selected = self.task_rows[previous]["id"] if 0 <= previous < len(self.task_rows) else None
        self.task_rows = self.store.tasks()
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

    def task_details(self) -> None:
        row = self.task_table.currentRow()
        if 0 <= row < len(self.task_rows):
            task = self.task_rows[row]
            self.task_detail.setPlainText(task["log"] + "\n结果\n" + readable_report(json.loads(task["result"])))

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
            self.refresh_backups()
        elif index == 6:
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
            values = [resource.name, readable_time(report.get("created_at", "未知")), str(report.get("file_count", "—")),
                      "Hermes 原生" if report.get("native") else readable_size(report.get("size", 0)), "待校验"]
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
        self.statusBar().showMessage("备份目录已保存")

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
                elif item.get("role") not in {"server-hermes-backup", "backup-archive"}:
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
            self.navigation.setCurrentRow(7)
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
        event.accept()
