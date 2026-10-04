from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import time
import unittest
from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from agent_manager.domain import KINDS, Resource
from agent_manager.storage import Store
from agent_manager.ui.dialogs import ResourceDialog
from agent_manager.ui.theme import setup_theme
from agent_manager.ui.window import MainWindow


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
        for index in range(8):
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


if __name__ == "__main__":
    unittest.main()
