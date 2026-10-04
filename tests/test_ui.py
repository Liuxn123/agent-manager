from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import time
import unittest
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QThread, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QTextEdit

from agent_manager.domain import KINDS, Resource
from agent_manager.storage import Store
from agent_manager.ui.dialogs import ResourceDialog
from agent_manager.ui.theme import setup_theme
from agent_manager.ui.window import MainWindow
from agent_manager.ui.library import HermesLibraryDialog
from agent_manager.runtime import TaskContext


class UITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        setup_theme(cls.app)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "data")
        self.window = MainWindow(self.store)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        deadline = time.monotonic() + 10
        while self.window.jobs and time.monotonic() < deadline:
            QTest.qWait(20)
        self.window.close()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_all_pages_navigate_and_forms_fit_at_minimum_window_size(self):
        from PySide6.QtGui import QRawFont
        self.assertTrue(QRawFont.fromFont(self.app.font()).supportsCharacter(ord("恢")))
        self.window.resize(960, 660)
        for index in range(9):
            self.window.navigation.setCurrentRow(index)
            self.app.processEvents()
            self.assertEqual(self.window.stack.currentIndex(), index)
            self.assertTrue(self.window.stack.currentWidget().isVisible())
        for kind in KINDS:
            dialog = ResourceDialog(self.window, kind)
            dialog.show()
            self.app.processEvents()
            self.assertGreater(dialog.fields["name"].height(), 25)
            self.assertGreater(dialog.fields["name"].width(), 100)
            dialog.close()

    def test_background_task_updates_persistence_on_gui_thread_and_controls_reenable(self):
        resource = Resource("Test project", "project", {"path": str(self.root)})
        self.store.save_resource(resource)
        self.window.refresh_resources()
        self.window.navigation.setCurrentRow(3)
        page = self.window.resource_pages[2]
        self.assertTrue(page.observe_button.isEnabled())

        callbacks = []
        identity = self.window.submit(resource, "observe", lambda context: {"okay": True},
            lambda result: callbacks.append((result, QThread.currentThread() == self.app.thread())))
        self.assertFalse(page.observe_button.isEnabled())
        deadline = time.monotonic() + 10
        while self.window.jobs and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertFalse(self.window.jobs)
        self.assertEqual(callbacks, [({"okay": True}, True)])
        self.assertEqual(self.store.tasks()[0]["state"], "success")
        self.assertTrue(page.observe_button.isEnabled())

    def test_reading_records_returns_content_without_persisting_it_in_task_history(self):
        outcomes = []
        self.window.submit(None, "浏览记录", lambda context: {"text": "fixture-private-conversation"}, outcomes.append, persist_result=False)
        deadline = time.monotonic() + 10
        while self.window.jobs and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertEqual(outcomes, [{"text": "fixture-private-conversation"}])
        task = self.store.tasks()[0]
        self.assertEqual(task["state"], "success")
        self.assertNotIn("fixture-private-conversation", task["result"] + task["log"])

    def test_management_labels_wrap_paths_are_complete_and_actions_do_not_overlap(self):
        location = str(self.root / ("带有较长名称的资料目录" * 8))
        resource = Resource("本机 Hermes · " + "需要完整显示的资源名称" * 4, "hermes_local", {"home": location})
        self.store.save_resource(resource)
        self.window.refresh_resources()
        self.window.navigation.setCurrentRow(1)
        page = self.window.resource_pages[0]
        for width in (960, 1280):
            self.window.resize(width, 700)
            QTest.qWait(120)
            self.assertEqual(page.restore_button.text(), "恢复备份")
            self.assertEqual(page.resources_table.textElideMode(), Qt.TextElideMode.ElideNone)
            self.assertGreater(page.resources_table.rowHeight(0), 45)
            controls = [page.observe_button, page.backup_button, page.restore_button, page.more_button, page.library_button]
            for first in controls:
                self.assertGreaterEqual(first.width(), first.sizeHint().width())
                self.assertLessEqual(first.geometry().right(), page.width())
                for second in controls:
                    if first is not second:
                        self.assertFalse(first.geometry().intersects(second.geometry()))
        page.show_paths()
        fields = page.paths_page.findChildren(QTextEdit, "DirectoryPath")
        self.assertIn(location, [field.toPlainText() for field in fields])
        copy = next(widget for widget in page.paths_page.findChildren(QPushButton) if widget.text() == "复制路径")
        copy.click()
        self.assertEqual(self.app.clipboard().text(), location)

    def test_resource_activity_filter_does_not_mix_resources(self):
        first = Resource("第一个项目", "project", {"path": str(self.root)})
        second = Resource("第二个项目", "project", {"path": str(self.root)})
        for resource in (first, second):
            self.store.save_resource(resource)
            self.store.start_task(resource.id, resource.id, resource.name + " · 检查")
        self.window.refresh_resources()
        self.window.show_resource_activity(first)
        self.assertEqual([row["resource_id"] for row in self.window.task_rows], [first.id])
        self.window.task_filter.setCurrentIndex(0)
        self.assertEqual(len(self.window.task_rows), 2)

    def test_hermes_dialog_reads_selected_content_without_persisting_conversations(self):
        resource = Resource("测试 Hermes", "hermes_local", {"home": str(self.root)})
        self.store.save_resource(resource)
        with closing(sqlite3.connect(self.root / "state.db")) as db, db:
            db.executescript("CREATE TABLE sessions(id TEXT,title TEXT,started_at REAL);"
                             "CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT);"
                             "INSERT INTO sessions VALUES('sample','测试会话',1);"
                             "INSERT INTO messages VALUES(1,'sample','user','fixture-private-Hermes-message');")
        report = self.window.service.action(resource, "library", TaskContext())
        dialog = HermesLibraryDialog(self.window, resource, report)
        dialog.show()
        dialog.tables["session"].selectRow(0)
        with patch("agent_manager.ui.window.QMessageBox.warning") as warnings:
            deadline = time.monotonic() + 10
            while "fixture-private-Hermes-message" not in dialog.preview.toPlainText() and time.monotonic() < deadline:
                QTest.qWait(20)
                time.sleep(0.005)
            self.assertFalse(warnings.called)
        self.assertIn("fixture-private-Hermes-message", dialog.preview.toPlainText())
        self.assertNotIn("fixture-private-Hermes-message", str(self.store.tasks()))
        dialog.reject()

    def test_agent_repository_button_runs_background_job_and_vault_git_is_explicit(self):
        repository = self.root / "agent-backups"
        self.store.set_setting("agent_backup_repository", str(repository))
        self.window.refresh_agent_repository()
        self.window.navigation.setCurrentRow(6)
        self.window.resize(960, 660)
        self.app.processEvents()
        self.assertIn(str(repository), self.window.agent_repository_label.text())
        self.assertGreater(self.window.agent_repository_label.width(), 150)
        with patch("agent_manager.ui.window.QMessageBox.information") as completed, patch("agent_manager.ui.window.QMessageBox.warning") as warnings:
            self.window.organize_agent_repository()
            deadline = time.monotonic() + 10
            while self.window.jobs and time.monotonic() < deadline:
                QTest.qWait(20)
                time.sleep(0.005)
            self.assertFalse(self.window.jobs)
            self.assertFalse(warnings.called, self.store.tasks()[0]["result"])
            self.assertEqual(self.store.tasks()[0]["state"], "success")
            self.assertIn("当前尚无 Agent 备份", completed.call_args[0][2])
        self.assertTrue((repository / "catalog.json").is_file())
        dialog = ResourceDialog(self.window, "vault")
        self.assertFalse(dialog.manage_git.isChecked())
        dialog.fields["path"].setText(str(self.root))
        dialog.manage_git.setChecked(True)
        dialog.save()
        self.assertTrue(dialog.result_resource.options["manage_git"])

    def test_obsidian_external_note_and_atomic_status_edits_refresh_original_documents(self):
        from agent_manager.project_workspaces import ProjectWorkspace
        from agent_manager.runtime import TaskContext
        from urllib.parse import parse_qs, urlparse
        workspace = ProjectWorkspace(self.root / "workspace")
        workspace.initialize(TaskContext())
        project = workspace.create("界面联动", "验证同一份资料", True, TaskContext())
        (workspace.root / "myself/.obsidian").mkdir()
        note = workspace.create_note(project["project_id"], "实验笔记", TaskContext())
        self.store.set_setting("project_workspace", str(workspace.root))
        self.window.navigation.setCurrentRow(3)
        page = self.window.project_page
        page.refresh()

        def until(predicate):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                QTest.qWait(30)
                time.sleep(0.005)  # Let Python workers run between Qt's synchronous test waits.
                if predicate() and not self.window.jobs:
                    return
            self.fail("Obsidian UI refresh did not complete: " + str({"rows": page.rows, "notes": page.note_list.count(), "preview": page.note_view.toPlainText(), "tasks": [(t['title'], t['state'], t['result']) for t in self.store.tasks()[:8]]}))

        until(lambda: page.note_list.count() == 1 and "实验笔记" in page.note_view.toPlainText())
        page.documents.setCurrentIndex(3)
        until(lambda: page.obsidian_button.isEnabled())
        with patch("agent_manager.ui.projects.QDesktopServices.openUrl", return_value=True) as opened:
            page.obsidian_button.click()
            until(lambda: opened.call_count == 1)
            params = parse_qs(urlparse(opened.call_args[0][0].toString()).query)
            entry = workspace.root / workspace.project(project["project_id"])["vault_entry"]
            self.assertEqual(params["path"], [str(entry / note["relative"])])
        marker = "fixture-external-note-edit-private"
        Path(note["path"]).write_text("# 外部编辑\n" + marker, encoding="utf-8")
        until(lambda: marker in page.note_view.toPlainText())
        status = Path(project["directory"]) / "agent/STATUS.md"
        replacement = status.with_suffix(".tmp")
        replacement.write_text(status.read_text(encoding="utf-8").replace("status: candidate", "status: paused"), encoding="utf-8")
        replacement.replace(status)
        until(lambda: page.rows and page.rows[0]["state"] == "paused")
        self.assertIn(str(status), page.watcher.files())
        self.assertFalse(any(marker in t["result"] + t["log"] for t in self.store.tasks()))
        self.window.resize(960, 660)
        self.app.processEvents()
        for control in (page.open_button, page.obsidian_button, page.move_button, page.new_note_button):
            self.assertTrue(control.isVisible())
            self.assertGreater(control.width(), 65)

    def test_obsidian_archived_notes_are_visible_without_create_or_inline_edit(self):
        from agent_manager.project_workspaces import ProjectWorkspace
        from agent_manager.runtime import TaskContext
        workspace = ProjectWorkspace(self.root / "workspace")
        workspace.initialize(TaskContext())
        project = workspace.create("历史笔记", "保持历史", False, TaskContext())
        workspace.create_note(project["project_id"], "保留笔记", TaskContext())
        workspace.move(workspace.plan_move(project["project_id"], False, TaskContext()), "归档", TaskContext())
        self.store.set_setting("project_workspace", str(workspace.root))
        self.window.navigation.setCurrentRow(3)
        page = self.window.project_page
        page.filter.setCurrentIndex(1)
        page.refresh()
        deadline = time.monotonic() + 10
        while (self.window.jobs or page.note_list.count() != 1) and time.monotonic() < deadline:
            QTest.qWait(30)
            time.sleep(0.005)
        page.documents.setCurrentIndex(3)
        deadline = time.monotonic() + 10
        while (self.window.jobs or page.note_list.count() != 1) and time.monotonic() < deadline:
            QTest.qWait(30)
            time.sleep(0.005)
        self.assertEqual(page.note_list.count(), 1)
        self.assertFalse(page.new_note_button.isEnabled())
        self.assertFalse(page.edit_button.isEnabled())
        self.assertFalse(page.log_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
