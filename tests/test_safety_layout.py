"""Selection and action routing checks for the two-column protection UI."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QMessageBox

from agent_manager.domain import Resource
from agent_manager.ui.safety import SafetyPage


class SafetyLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.project = Resource("示例项目", "project", {"path": "C:/fixture/project"})
        self.agent = Resource("开发 Agent", "agent", {"record_paths": ["C:/fixture/records"]})
        self.hermes = Resource("服务器 Hermes", "hermes_server", {"home": "/home/demo/.hermes"})
        self.capabilities = {"backup", "restore", "verify", "observe", "open"}
        self.window = SimpleNamespace(store=Mock(), is_busy=Mock(return_value=False),
            perform=Mock(), edit_resource=Mock(), remove_resource=Mock(), add_resource=Mock(),
            service=SimpleNamespace(registry=SimpleNamespace(get=lambda resource: SimpleNamespace(capabilities=self.capabilities)),
                                    backup_root=lambda: Path("C:/fixture/backups")))
        with patch.object(SafetyPage, "refresh"):
            self.page = SafetyPage(self.window)
        self.addCleanup(self.page.deleteLater)
        self.page.entries = [
            {"resource": self.project, "health": {"state": "待校验", "created_at": "2026-10-15T10:00:00", "source": "C:/fixture/a.amb"},
             "versions": [{"archive": "C:/fixture/a.amb", "created_at": "2026-10-15T10:00:00", "integrity": "待校验"},
                          {"archive": "C:/fixture/b.amb", "created_at": "2026-10-14T10:00:00", "integrity": "已校验"}]},
            {"resource": self.agent, "health": {"state": "未备份", "created_at": None}, "versions": []},
            {"resource": self.hermes, "health": {"state": "已校验", "created_at": "2026-10-15T10:00:00", "verified": True}, "versions": []},
        ]
        self.page._fill_overview()

    def test_selection_and_filtering_do_not_execute_backup_actions(self):
        self.page.search.setText("Agent")
        self.assertEqual(self.page.resources_list.count(), 1)
        self.assertEqual(self.page._selected_entry()["resource"].id, self.agent.id)
        self.page.show_restore(self.project)
        self.assertEqual(self.page.search.text(), "")
        self.assertEqual(self.page.views.currentIndex(), 1)
        self.assertEqual(self.page._selected_entry()["resource"].id, self.project.id)
        self.window.perform.assert_not_called()

    def test_selected_source_and_version_survive_refresh(self):
        self.page.show_restore(self.project)
        self.page.version_list.setCurrentRow(1)
        self.page.entries.reverse()
        self.page._fill_overview()
        self.assertEqual(self.page._selected_entry()["resource"].id, self.project.id)
        self.page._continue_restore()
        self.window.perform.assert_called_once_with(self.project, "restore", "C:/fixture/b.amb")

    def test_empty_versions_cannot_trigger_restore(self):
        self.page.show_restore(self.agent)
        self.assertFalse(self.page.continue_restore.isEnabled())
        with patch.object(QMessageBox, "information"):
            self.page._continue_restore()
        self.window.perform.assert_not_called()
        self.assertEqual(self.page.summary[0].text(), "尚无备份")

    def test_primary_action_routes_to_selected_resource(self):
        self.page.show_overview(self.project)
        self.page._run_primary_action()
        self.window.perform.assert_called_once_with(self.project, "verify", "C:/fixture/a.amb")
        self.window.perform.reset_mock()
        self.page.show_overview(self.agent)
        self.page._run_primary_action()
        self.window.perform.assert_called_once_with(self.agent, "backup", "")

    def test_native_restore_and_busy_guards_preserved(self):
        self.page.show_restore(self.hermes)
        self.assertIn("原生", self.page.restore_note.text())
        self.page._continue_restore()
        self.window.perform.assert_called_once_with(self.hermes, "restore", "")
        self.window.perform.reset_mock()
        self.window.is_busy.return_value = True
        self.page._render_selected()
        self.assertFalse(self.page.continue_restore.isEnabled())
        self.assertFalse(self.page.primary_button.isEnabled())
        self.page._continue_restore()
        self.page._run_primary_action()
        self.window.perform.assert_not_called()

    def test_filters_and_no_match_clear_detail_actions(self):
        self.page.state_filter.setCurrentIndex(self.page.state_filter.findData("healthy"))
        self.assertEqual(self.page._selected_entry()["resource"].id, self.hermes.id)
        self.page.state_filter.setCurrentIndex(self.page.state_filter.findData("unbacked"))
        self.assertEqual(self.page._selected_entry()["resource"].id, self.agent.id)
        self.page.search.setText("no-match")
        self.assertIsNone(self.page._selected_entry())
        self.assertFalse(self.page.primary_button.isEnabled())
        self.assertFalse(self.page.continue_restore.isEnabled())
        self.assertEqual(self.page.source_path.text(), "")

    def test_external_resource_navigation_clears_filters_and_selects_correct_item(self):
        self.page.kind_filter.setCurrentIndex(self.page.kind_filter.findData("agent"))
        self.page.show_overview(self.project)
        self.assertEqual(self.page.kind_filter.currentData(), "all")
        self.assertEqual(self.page._selected_entry()["resource"].id, self.project.id)
        self.assertEqual(self.page.views.currentIndex(), 0)
        self.page.show_restore()
        self.assertEqual(self.page._selected_entry()["resource"].id, self.project.id)
        self.assertEqual(self.page.views.currentIndex(), 1)


if __name__ == "__main__":
    unittest.main()
