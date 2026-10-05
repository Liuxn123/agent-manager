import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QDialog

from agent_manager import archives
from agent_manager.domain import Resource
from agent_manager.runtime import TaskContext
from agent_manager.storage import Store
from agent_manager.ui.dialogs import TransferDialog, PlanDialog
from agent_manager.ui.theme import setup_theme
from agent_manager.ui.window import MainWindow


class ManagementUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        setup_theme(cls.app)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.store = Store(self.root / "data")
        self.window = MainWindow(self.store)
        self.window.show()
        self.app.processEvents()

    def wait_jobs(self):
        deadline = time.monotonic() + 15
        while self.window.jobs and time.monotonic() < deadline:
            QTest.qWait(20)
            time.sleep(0.005)
        self.assertFalse(self.window.jobs)

    def tearDown(self):
        self.wait_jobs()
        self.window.close()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_simplified_settings_change_destination_without_moving_existing_backup(self):
        self.window.navigation.setCurrentRow(self.window.SETTINGS)
        self.app.processEvents()
        self.assertFalse(self.window.settings_advanced.isVisible())
        self.assertFalse(self.window.backup_keep.isVisible())
        old_root = self.window.service.backup_root()
        old_root.mkdir(parents=True, exist_ok=True)
        (old_root / "existing.amb").write_bytes(b"fixture-existing-backup")
        destination = self.root / "new-backups"
        destination.mkdir()
        with patch("agent_manager.ui.window.QFileDialog.getExistingDirectory", return_value=str(destination)):
            self.window.change_backup_location()
        self.assertEqual(self.window.service.backup_root().resolve(), destination.resolve())
        self.assertEqual((old_root / "existing.amb").read_bytes(), b"fixture-existing-backup")
        self.assertIn(str(destination.resolve()), self.window.backup_location.toPlainText())
        self.window.settings_advanced_toggle.setChecked(True)
        self.app.processEvents()
        self.assertTrue(self.window.backup_keep.isVisible())

    def test_dashboard_focuses_missing_backups_and_failed_operations(self):
        resource = Resource("待处理项目", "project", {"path": str(self.root)})
        self.store.save_resource(resource)
        self.store.start_task("fixturefailed", resource.id, "恢复预览")
        self.store.finish_task("fixturefailed", "failed", {"error": "fixture failure"})
        self.window.refresh_resources()
        self.assertIn("最近操作失败或中断", self.window.pending_items.item(0).toolTip())
        self.window.open_dashboard_item(self.window.pending_items.item(0))
        self.assertEqual(self.window.navigation.currentRow(), self.window.SAFETY)
        self.assertEqual(self.window.task_rows[0]["resource_id"], resource.id)
        self.assertNotIn("搜索本地记录", [button.text() for button in self.window.stack.widget(0).findChildren(QPushButton)])

    def test_new_computer_restore_gui_pipeline_restores_files_database_and_registration(self):
        project, records = self.root / "source-project", self.root / "source-records"
        project.mkdir()
        records.mkdir()
        (project / "成果.md").write_text("中文成果", encoding="utf-8")
        (records / "chat.jsonl").write_text('{"role":"user","content":"fixture-chat-history"}', encoding="utf-8")
        (records / "attachment.bin").write_bytes(bytes(range(256)))
        with closing(sqlite3.connect(records / "state.sqlite")) as db, db:
            db.execute("CREATE TABLE sessions(title TEXT)")
            db.execute("INSERT INTO sessions VALUES('fixture-database-history')")
        original = Resource("跨电脑测试 Agent", "agent", {"engine": "其他 Agent", "path": str(project), "record_paths": [str(records)], "portable_bundle": True})
        password = "fixture-backup-password"
        backup = archives.create_archive(original, self.root / "transfer-backups", password, TaskContext())
        target = self.root / "fresh-computer"
        def choose(dialog):
            dialog.target.setText(str(target))
            for key, field in dialog.folder_fields.items():
                field.setText("新项目" if key == "project" else "新记录")
            return QDialog.DialogCode.Accepted
        with patch.object(self.window, "password_input", return_value=(password, False)), patch.object(TransferDialog, "exec", choose), \
             patch.object(PlanDialog, "exec", return_value=QDialog.DialogCode.Accepted), patch("agent_manager.ui.window.QMessageBox.information") as done, \
             patch("agent_manager.ui.window.QMessageBox.warning") as warnings:
            self.window.import_backup(backup["archive"])
            self.wait_jobs()
            self.assertFalse(warnings.called, str(self.store.tasks()))
            self.assertTrue(done.called)
        self.assertEqual((target / "新项目/成果.md").read_text(encoding="utf-8"), "中文成果")
        self.assertEqual((target / "新记录/attachment.bin").read_bytes(), bytes(range(256)))
        with closing(sqlite3.connect(target / "新记录/state.sqlite")) as db:
            self.assertEqual(db.execute("SELECT title FROM sessions").fetchone()[0], "fixture-database-history")
        restored = self.store.resources()[0]
        self.assertEqual([Path(path).resolve() for path in restored.options["record_paths"]], [(target / "新记录").resolve()])
        self.assertNotIn("executable", restored.options)
        self.assertNotIn("fixture-chat-history", str(self.store.tasks()))
