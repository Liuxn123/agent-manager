from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QTextEdit, QVBoxLayout, QWidget, QListWidget, QGroupBox, QSpinBox)

from ..domain import KINDS, Resource, RestorePlan, UserError
from ..security import safe_result
from .presentation import readable_report
from ..profiles import ENGINES, RECORD_NOTE, discover_record_paths


def choose_path(edit: QLineEdit, parent: QWidget, directory: bool = True) -> None:
    current = edit.text() or str(Path.home())
    value = QFileDialog.getExistingDirectory(parent, "选择目录", current) if directory else QFileDialog.getOpenFileName(parent, "选择文件", current)[0]
    if value:
        edit.setText(value)


class ResourceDialog(QDialog):
    def __init__(self, parent: QWidget, kind: str, resource: Resource | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.original = resource
        self.result_resource: Resource | None = None
        self.fields: dict[str, QLineEdit] = {}
        self.setWindowTitle(("编辑" if resource else "添加") + KINDS[kind])
        self.resize(660, 680 if kind == "hermes_server" else 550)
        self.setMinimumSize(540, 380)
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        panel = QWidget()
        self.form = QFormLayout(panel)
        self.form.setSpacing(12)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        scroll.setWidget(panel)
        layout.addWidget(scroll)
        self.add_field("name", "名称", resource.name if resource else KINDS[kind])
        options = resource.options if resource else {}
        if kind == "hermes_local":
            self.add_field("home", "Hermes 运行目录", options.get("home", str(Path.home() / ".hermes")), "dir")
            self.add_field("backup_repo", "Hermes 备份仓库", options.get("backup_repo", ""), "dir")
            self.add_field("python", "工具 Python", options.get("python", self.default_python()), "file")
            self.add_field("passphrase_file", "受保护口令文件", options.get("passphrase_file", ""), "file")
            self.add_field("workspace", "Workspace（可选）", options.get("workspace", ""), "dir")
            self.add_field("executable", "启动程序（可选）", options.get("executable", ""), "file")
            self.add_field("arguments", "启动参数 JSON 数组", json.dumps(options.get("arguments", []), ensure_ascii=False))
            self.push = QCheckBox("备份后通过现有工具提交并推送 Git")
            self.push.setChecked(options.get("push", True))
            self.form.addRow("", self.push)
            note = "调用现有 Hermes 备份仓库工具。恢复前退出 Hermes，口令文件由你在本机选择，内容不会写入资源配置。"
        elif kind == "hermes_server":
            for key, label, default, chooser in [
                ("host", "SSH 别名 / user@host", "", ""), ("port", "SSH 端口", "22", ""),
                ("user", "SSH 登录账号（别名已配置可留空）", "", ""),
                ("identity_file", "私钥文件（可选）", "", "file"), ("ssh_config", "SSH 配置（可选）", "", "file"),
                ("known_hosts", "主机指纹文件（可选）", "", "file"),
                ("python", "远端 Python", "python3", ""), ("home", "远端 Hermes 目录", "/home/hermes/.hermes", ""),
                ("backup_repo", "远端备份仓库", "/home/hermes/hermes-server-backup", ""),
                ("knowledge_repo", "远端知识库", "/home/hermes/hermes-knowledge-base", ""),
                ("run_user", "备份运行账号", "hermes", ""), ("service", "网关服务名", "hermes-gateway.service", ""),
                ("passphrase_file", "远端口令文件（可选）", "", "")]:
                self.add_field(key, label, str(options.get(key, default)), chooser)
            self.scope = QComboBox()
            self.scope.addItem("系统服务", "system")
            self.scope.addItem("用户服务", "user")
            self.scope.setCurrentIndex(1 if options.get("service_scope") == "user" else 0)
            self.form.addRow("服务范围", self.scope)
            reuse = QPushButton("读取已有服务器连接参数")
            def import_connection():
                from ..adapters.server import existing_connection
                for key, value in existing_connection().items():
                    if key in self.fields:
                        self.fields[key].setText(str(value))
            reuse.clicked.connect(import_connection)
            self.form.addRow("", reuse)
            note = "管理 Linux/systemd 服务器；使用 SSH Agent 或私钥，严格验证主机指纹。恢复只写入空目录，不自动启动网关。"
        else:
            self.add_field("path", "项目文件夹（Agent 可不填）" if kind == "agent" else "项目文件夹", options.get("path", ""), "dir")
            self.add_field("exclude_dirs", "额外排除目录名", ", ".join(options.get("exclude_dirs", [])))
            if kind == "vault":
                self.manage_git = QCheckBox("独立管理知识库 Git（例如 myself）")
                self.manage_git.setChecked(bool(options.get("manage_git")))
                self.form.addRow("", self.manage_git)
            if kind == "agent":
                self.engine = QComboBox()
                self.engine.addItems(ENGINES)
                self.engine.setCurrentText(options.get("engine", "Codex") if not resource or options.get("engine") in ENGINES else "其他 Agent")
                if not resource:
                    self.fields["name"].setText("我的 Codex 资料")
                def engine_changed(value):
                    if not resource and self.fields["name"].text() in {KINDS[kind], *("我的 " + engine + " 资料" for engine in ENGINES)}:
                        self.fields["name"].setText("我的 " + value + " 资料")
                self.engine.currentTextChanged.connect(engine_changed)
                self.form.addRow("使用哪个 Agent", self.engine)
                self.record_paths = QListWidget()
                self.record_paths.setMaximumHeight(105)
                self.record_paths.addItems(options.get("record_paths", []))
                self.form.addRow("本地记录 / 配置目录", self.record_paths)
                controls = QWidget()
                controls_layout = QHBoxLayout(controls)
                controls_layout.setContentsMargins(0, 0, 0, 0)
                for label, callback in [("自动找记录目录", self.discover_records), ("添加目录", self.add_record_path), ("移除选中目录", self.remove_record_path)]:
                    control = QPushButton(label)
                    control.clicked.connect(callback)
                    controls_layout.addWidget(control)
                self.form.addRow("", controls)
                advanced = QGroupBox("可选：从管家启动 Agent（备份无需设置）")
                advanced.setCheckable(True)
                advanced.setChecked(bool(options.get("executable")))
                advanced_form = QFormLayout(advanced)
                saved_form = self.form
                self.form = advanced_form
                self.add_field("executable", "程序路径", options.get("executable", ""), "file")
                self.add_field("arguments", "启动参数（JSON 数组）", json.dumps(options.get("arguments", []), ensure_ascii=False))
                self.form = saved_form
                self.form.addRow(advanced)
                self.resize(760, 680)
            note = ("选项目文件夹，保存代码与成果；再添加本地记录目录，保存聊天、配置和附件。备份前退出对应 Agent。" + RECORD_NOTE) if kind == "agent" else "代码、笔记、附件及未提交文件会一起加密备份。换电脑时恢复到新文件夹。"
        hint = QLabel(note)
        hint.setWordWrap(True)
        hint.setObjectName("Subtitle")
        layout.addWidget(hint)
        if kind != "hermes_server":
            self.automatic = QCheckBox("管家打开时，每天自动备份这项资料")
            self.automatic.setChecked(bool(options.get("automatic_backup")))
            self.automatic.setToolTip("需要已保存的备份口令；运行中的 Agent 将延后备份。旧版保留数量在设置中调整。")
            layout.addWidget(self.automatic)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存资源")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def default_python() -> str:
        return (shutil.which("python3") or shutil.which("python") or "python") if getattr(sys, "frozen", False) else sys.executable

    def add_field(self, key: str, label: str, value: str = "", chooser: str = "") -> None:
        edit = QLineEdit(str(value))
        self.fields[key] = edit
        edit.setAccessibleName(label)
        if chooser:
            row = QWidget()
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(edit)
            button = QPushButton("选择")
            button.clicked.connect(lambda checked=False: choose_path(edit, self, chooser == "dir"))
            layout.addWidget(button)
            self.form.addRow(label, row)
        else:
            self.form.addRow(label, edit)

    def save(self) -> None:
        try:
            values = {key: edit.text().strip() for key, edit in self.fields.items()}
            name = values.pop("name")
            if not name:
                raise UserError("请输入资源名称。")
            options = {**(self.original.options if self.original else {}), **values}
            if self.kind != "hermes_server":
                options["automatic_backup"] = self.automatic.isChecked()
                options.setdefault("backup_interval_hours", 24)
            if self.kind == "hermes_server":
                from ..adapters.server import ssh_command
                options["port"] = int(options["port"])
                options["service_scope"] = self.scope.currentData()
                ssh_command(Resource(name, self.kind, options), {"action": "observe"})
                for key in ("home", "backup_repo", "knowledge_repo"):
                    if not str(options[key]).startswith("/"):
                        raise UserError("服务器目录需要绝对路径。")
            elif self.kind == "hermes_local":
                for key in ("home", "backup_repo"):
                    if not options[key]:
                        raise UserError("请设置运行目录和备份仓库。")
                options["push"] = self.push.isChecked()
                arguments = json.loads(values["arguments"])
                if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
                    raise UserError("启动参数需要字符串 JSON 数组。")
                options["arguments"] = arguments
            else:
                if self.kind != "agent" and not options["path"]:
                    raise UserError("请选择资源目录。")
                options["exclude_dirs"] = [part.strip() for part in values["exclude_dirs"].split(",") if part.strip()]
                if self.kind == "vault":
                    options["manage_git"] = self.manage_git.isChecked()
                if self.kind == "agent":
                    options["engine"] = self.engine.currentText()
                    options["record_paths"] = [self.record_paths.item(index).text() for index in range(self.record_paths.count())]
                    options["portable_bundle"] = True
                    if not options["path"] and not options["record_paths"]:
                        raise UserError("请选择项目文件夹，或添加一个本地记录目录。")
                    arguments = json.loads(values["arguments"])
                    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
                        raise UserError("启动参数需要字符串 JSON 数组，例如 [\"-m\", \"my_agent\"]。")
                    options["arguments"] = arguments
            self.result_resource = Resource(name, self.kind, options, self.original.id) if self.original else Resource(name, self.kind, options)
            self.accept()
        except (UserError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "配置未保存", str(exc))

    def discover_records(self) -> None:
        paths = discover_record_paths(self.engine.currentText())
        if not paths:
            QMessageBox.information(self, "未找到记录目录", "可在原应用中查看数据保存位置，再用“添加目录”选择。WorkBuddy 的工作成果文件夹可填在“项目文件夹”中。")
        existing = {self.record_paths.item(index).text() for index in range(self.record_paths.count())}
        for path in paths:
            if str(path) not in existing:
                self.record_paths.addItem(str(path))

    def add_record_path(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择保存聊天记录或配置的目录", str(Path.home()))
        if path and path not in {self.record_paths.item(index).text() for index in range(self.record_paths.count())}:
            self.record_paths.addItem(path)

    def remove_record_path(self) -> None:
        self.record_paths.takeItem(self.record_paths.currentRow())


class PasswordDialog(QDialog):
    def __init__(self, parent: QWidget, creating: bool) -> None:
        super().__init__(parent)
        self.setWindowTitle("加密备份口令" if creating else "解锁备份")
        self.resize(480, 230)
        layout = QVBoxLayout(self)
        hint = QLabel(("设置一个备份口令，用来保护项目与记录。" if creating else "输入创建这份备份时使用的口令。") + "这不是 Agent 的登录密码。请单独保存口令，换电脑恢复时需要它。已为这项资料保存口令时，可以留空。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("口令", self.password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        if creating:
            form.addRow("再次输入", self.confirm)
        layout.addLayout(form)
        portable = bool(getattr(getattr(parent, "store", None), "portable_root", None))
        self.remember = QCheckBox("保存至加密便携口令库（先在设置中解锁）" if portable else "保存至这台电脑的系统凭据存储")
        layout.addWidget(self.remember)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("继续")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        def accept_password():
            if creating and self.password.text() != self.confirm.text():
                QMessageBox.warning(self, "口令不一致", "请重新输入相同口令。")
            else:
                self.accept()
        buttons.accepted.connect(accept_password)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ArchiveRestoreDialog(QDialog):
    def __init__(self, parent: QWidget, source: str, target: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择恢复资料与目标")
        self.resize(620, 240)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.source = QLineEdit(source)
        self.target = QLineEdit(target)
        for label, edit, directory in [("加密备份 .amb", self.source, False), ("新目录 / 空目录", self.target, True)]:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(edit)
            button = QPushButton("选择")
            button.clicked.connect(lambda checked=False, edit=edit, directory=directory: choose_path(edit, self, directory))
            row_layout.addWidget(button)
            form.addRow(label, row)
        layout.addLayout(form)
        hint = QLabel("可以直接输入尚未创建的新目录。恢复会先校验全部内容，不覆盖已有资料。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("校验并预览")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class PlanDialog(QDialog):
    def __init__(self, parent: QWidget, resource: Resource, plan: RestorePlan) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认恢复计划")
        self.resize(700, 570)
        layout = QVBoxLayout(self)
        title = QLabel(f"恢复 {resource.name}")
        title.setObjectName("Title")
        layout.addWidget(title)
        target = QLabel("目标：" + plan.target)
        target.setWordWrap(True)
        layout.addWidget(target)
        summary = QTextEdit()
        summary.setReadOnly(True)
        summary.setPlainText(readable_report(plan.summary))
        layout.addWidget(summary)
        self.confirm = QCheckBox("已核对恢复位置，并退出正在写入这些资料的应用")
        layout.addWidget(self.confirm)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        apply = buttons.button(QDialogButtonBox.StandardButton.Ok)
        apply.setText("执行恢复")
        apply.setProperty("primary", True)
        apply.setEnabled(False)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.confirm.toggled.connect(apply.setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class TransferDialog(QDialog):
    def __init__(self, parent: QWidget, report: dict, target: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("换电脑恢复 · 选择放在哪里")
        self.resize(740, 470)
        self.folder_fields = {}
        layout = QVBoxLayout(self)
        heading = QLabel(f"已解锁：{report['resource_name']}（{report['file_count']} 个文件）")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        hint = QLabel("选择一个新的总文件夹。项目文件和本地记录分别放在它里面；下方可以修改各自的文件夹名称。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.target = QLineEdit(target)
        self.target.setAccessibleName("新电脑上的恢复总文件夹")
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(self.target)
        choose = QPushButton("选择")
        choose.clicked.connect(lambda: choose_path(self.target, self))
        row_layout.addWidget(choose)
        form.addRow("恢复总文件夹", row)
        for item in report.get("components", []):
            name = "项目文件" if item["role"] == "project" else ("本地记录" if item["id"] == "records" else "本地记录-" + item["id"])
            edit = QLineEdit(name)
            edit.setAccessibleName(item["label"] + "文件夹名称")
            self.folder_fields[item["id"]] = edit
            form.addRow(item["label"], edit)
        layout.addLayout(form)
        note = QLabel(RECORD_NOTE + "\n聊天记录查看支持本地文本、Markdown、JSON 和 JSONL；应用内部数据库保留原样。")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("查看恢复清单")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class RecordsDialog(QDialog):
    def __init__(self, window: QWidget, resource: Resource, report: dict) -> None:
        super().__init__(window)
        self.setWindowTitle(resource.name + " · 浏览本地记录")
        self.resize(1000, 680)
        layout = QVBoxLayout(self)
        hint = QLabel("这是本机保存的记录，阅读不会改动原应用。数据库格式的记录请在原应用中查看。" + ("仅列出前 300 个文件。" if report.get("limited") else ""))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.rows = report["records"]
        self.files = QListWidget()
        self.files.addItems([item["component"] + " · " + item["name"] for item in self.rows])
        self.files.setMaximumHeight(180)
        layout.addWidget(self.files)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText("选择上方记录查看内容。" if self.rows else "没有找到可直接阅读的文本记录。资料仍可备份；请核对原应用的数据目录，或先从原应用导出记录。")
        layout.addWidget(self.text, 1)
        def select(row):
            if row < 0:
                return
            from ..records import read_record
            window.submit(resource, "读取本地记录", lambda context: read_record(resource, self.rows[row]["path"], context),
                          lambda result: self.text.setPlainText(result["text"]) if self.isVisible() else None, persist_result=False)
        self.files.currentRowChanged.connect(select)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        layout.addWidget(close)
