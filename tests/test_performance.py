from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from agent_manager.adapters.agents import AgentAdapter
from agent_manager.domain import Resource
from agent_manager.process_inventory import process_names
from agent_manager.project_workspaces import ProjectWorkspace
from agent_manager.runtime import TaskContext
from agent_manager.storage import Store
from agent_manager.ui.window import MainWindow


class ProcessInventoryTests(unittest.TestCase):
    def test_shared_inventory_retains_native_and_node_agents_without_reading_unrelated_commands(self):
        codex = Resource("Codex", "agent", {"engine": "Codex"})
        claude = Resource("Claude", "agent", {"engine": "Claude Code"})
        node = Mock()
        node.cmdline.return_value = ["node", r"C:\tools\@anthropic-ai\claude-code\cli.js"]
        with patch("agent_manager.adapters.agents.process_names", return_value=[(10, "codex.exe"), (11, "node.exe"), (12, "unrelated.exe")]) as inventory, patch("psutil.Process", return_value=node) as process:
            result = AgentAdapter().external_pids_many([codex, claude])
        self.assertEqual(result, {codex.id: [10], claude.id: [11]})
        inventory.assert_called_once()
        process.assert_called_once_with(11)

    def test_native_inventory_contains_the_current_process(self):
        self.assertIn(os.getpid(), {pid for pid, name in process_names()})


class PerformanceUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "data")
        self.window = MainWindow(self.store)
        self.window.show()
        self.until(lambda: self.window.agent_status_worker is None)

    def until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(10)
            time.sleep(.002)
        self.fail("Background operation did not complete")

    def tearDown(self):
        self.until(lambda: not self.window.jobs and self.window.agent_status_worker is None)
        self.window.close()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_slow_status_scan_runs_off_gui_thread_and_does_not_queue_duplicates(self):
        resource = Resource("Codex", "agent", {"engine": "Codex", "record_paths": [str(self.root)]})
        self.store.save_resource(resource)
        adapter = self.window.service.registry.get(resource)
        self.window.navigation.setCurrentRow(self.window.SAFETY)
        self.window.safety_tabs.setCurrentIndex(3)
        self.until(lambda: self.window.agent_status_worker is None)
        gate, entered = threading.Event(), threading.Event()
        threads, ticks = [], []
        def scan(resources, cancel):
            threads.append(threading.get_ident())
            entered.set()
            gate.wait(10)
            return {resource.id: [123]}
        timer = QTimer(); timer.setInterval(5); timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        with patch.object(adapter, "external_pids_many", side_effect=scan) as inventory:
            try:
                self.window.refresh_agent_activity()
                worker = self.window.agent_status_worker
                self.window.refresh_agent_activity()
                self.assertIs(self.window.agent_status_worker, worker)
                self.until(entered.is_set)
                ticks.clear()
                # macOS offscreen timers may coalesce; prove responsiveness while the
                # worker remains blocked, without assuming a 5 ms wall-clock cadence.
                self.until(lambda: len(ticks) > 3)
                self.assertNotEqual(threads, [threading.get_ident()])
            finally:
                gate.set()
                self.until(lambda: self.window.agent_status_worker is None)
                timer.stop()
            inventory.assert_called_once()

    def test_idle_history_timer_is_stopped_and_document_reads_do_not_refresh_all_pages(self):
        self.assertFalse(self.window.timer.isActive())
        with patch.object(self.window, "refresh_resources") as refresh, patch.object(self.window, "refresh_dashboard") as dashboard:
            self.window.submit(None, "读取文档", lambda context: {"text": "fixture"}, persist_result=False)
            self.until(lambda: not self.window.jobs)
            refresh.assert_not_called()
            dashboard.assert_not_called()
        self.assertFalse(self.window.timer.isActive())

    def test_history_loads_only_selected_detail_and_preserves_unchanged_cells(self):
        self.store.start_task("first", "system", "日志测试")
        self.store.append_log("first", "fixture-log")
        self.store.finish_task("first", "success", {"fixture": True})
        with patch.object(self.store, "tasks", side_effect=AssertionError("Full history should not be loaded")):
            self.window.refresh_tasks()
            cell = self.window.task_table.item(0, 0)
            self.window.refresh_tasks()
        self.assertIs(self.window.task_table.item(0, 0), cell)
        self.assertIn("fixture-log", self.window.task_detail.toPlainText())
        self.assertNotIn("log", self.window.task_rows[0])

    def test_project_tabs_reuse_loaded_documents(self):
        workspace = ProjectWorkspace(self.root / "workspace")
        workspace.initialize(TaskContext())
        workspace.create("性能验证", "验证页面切换", False, TaskContext())
        self.store.set_setting("project_workspace", str(workspace.root))
        self.window.navigation.setCurrentRow(self.window.PROJECT)
        self.until(lambda: not self.window.jobs)
        page = self.window.project_page
        version, history = page.document_version, len(self.store.tasks())
        for index in (1, 2, 3, 0):
            page.documents.setCurrentIndex(index)
        self.assertEqual(page.document_version, version)
        self.assertFalse(self.window.jobs)
        self.assertEqual(len(self.store.tasks()), history)


if __name__ == "__main__":
    unittest.main()
