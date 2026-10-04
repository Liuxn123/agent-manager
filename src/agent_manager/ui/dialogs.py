from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QTextEdit, QVBoxLayout, QWidget)

from ..domain import KINDS, Resource, RestorePlan, UserError
from ..security import safe_result
from .presentation import readable_report


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
                ("identity_file", "私钥文件（可选）", "", "file"), ("ssh_config", "SSH 配置（可选）", "", "file"),
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
            note = "管理 Linux/systemd 服务器；使用 SSH Agent 或私钥，严格验证主机指纹。恢复只写入空目录，不自动启动网关。"
        else:
            self.add_field("path", "资源目录", options.get("path", ""), "dir")
            self.add_field("exclude_dirs", "额外排除目录名", ", ".join(options.get("exclude_dirs", [])))
            if kind == "agent":
                self.add_field("engine", "Agent 类型", options.get("engine", "通用 Agent"))
                self.add_field("executable", "启动可执行文件", options.get("executable", ""), "file")
                self.add_field("arguments", "启动参数 JSON 数组", json.dumps(options.get("arguments", []), ensure_ascii=False))
            note = "项目、附件、配置与未提交文件将加密保存；依赖和缓存目录默认排除。备份前关闭写入程序，恢复到新目录。"
        hint = QLabel(note)
        hint.setWordWrap(True)
        hint.setObjectName("Subtitle")
        layout.addWidget(hint)
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
                if not options["path"]:
                    raise UserError("请选择资源目录。")
                options["exclude_dirs"] = [part.strip() for part in values["exclude_dirs"].split(",") if part.strip()]
                if self.kind == "agent":
                    arguments = json.loads(values["arguments"])
                    if not isinstance(arguments, list) or not all(isinstance(item, str) for item in arguments):
                        raise UserError("启动参数需要字符串 JSON 数组，例如 [\"-m\", \"my_agent\"]。")
                    options["arguments"] = arguments
            self.result_resource = Resource(name, self.kind, options, self.original.id) if self.original else Resource(name, self.kind, options)
            self.accept()
        except (UserError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "配置未保存", str(exc))


class PasswordDialog(QDialog):
    def __init__(self, parent: QWidget, creating: bool) -> None:
        super().__init__(parent)
        self.setWindowTitle("加密备份口令" if creating else "解锁备份")
        self.resize(480, 230)
        layout = QVBoxLayout(self)
        hint = QLabel("口令用于加密资料，请另存于密码管理器。留空可使用本资源已保存的系统凭据。换电脑需要独立保存的口令。")
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
        self.remember = QCheckBox("保存至这台电脑的系统凭据存储")
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
        self.confirm = QCheckBox("已核对目标与恢复范围；本地 Hermes 已退出，相关写入程序已停止")
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
