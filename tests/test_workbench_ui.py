import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import time
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt, QPoint, QDate
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QComboBox, QListWidget, QTextEdit, QVBoxLayout

from agent_manager.domain import Resource
from agent_manager.storage import Store
from agent_manager.runtime import TaskContext
from agent_manager.project_workspaces import ProjectWorkspace
from agent_manager.workbench import Daily, Catalog, work_root, project_context, summaries
from agent_manager.ui.window import MainWindow
from agent_manager.ui.workbench import CatalogDialog
from agent_manager.ui.agenda import AgendaPanel


class WorkbenchUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = ProjectWorkspace(self.root / "workspace")
        self.workspace.initialize(TaskContext())
        self.project = self.workspace.create("每日工作", "走通工作入口", True, TaskContext())
        (self.workspace.root / "myself/.obsidian").mkdir()
        self.store = Store(self.root / "data")
        self.store.set_setting("project_workspace", str(self.workspace.root))
        self.agent = Resource("我的 Codex", "agent", {"engine": "Codex", "record_paths": [str(self.root)], "portable_bundle": True})
        self.store.save_resource(self.agent)
        self.daily = Daily(work_root(self.store))
        self.daily.save(self.daily.template().replace("## 日程\n", "## 日程\n- 09:00 开发\n").replace("## 今日任务\n", "## 今日任务\n- [ ] 验证恢复\n"), None)
        Catalog(work_root(self.store)).save({"name": "阶段总结 Prompt", "type": "prompt", "projects": [self.project["project_id"]], "agents": [self.agent.id], "tags": ["总结"]}, "人工可编辑正文")
        self.window = MainWindow(self.store)
        self.window.show()
        self.until(lambda: self.window.today_page.report is not None)

    def until(self, predicate):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate() and not self.window.jobs:
                return
            QTest.qWait(15)
            time.sleep(.003)
        self.fail("UI operation timed out: " + str([(t["title"], t["state"], t["result"]) for t in self.store.tasks()[:5]]))

    def tearDown(self):
        self.until(lambda: not self.window.jobs and not self.window.today_page.worker and not self.window.catalog_page.worker and not self.window.agent_status_worker)
        self.window.close()
        self.window.pool.waitForDone(5000)
        self.app.processEvents()
        self.temp.cleanup()

    def test_daily_is_default_and_checkbox_and_obsidian_edit_share_original_file(self):
        self.assertEqual([self.window.navigation.item(i).text() for i in range(6)], ["今日", "项目", "Agent", "资源库", "数据安全", "设置"])
        self.assertIs(self.window.stack.currentWidget(), self.window.today_page)
        page = self.window.today_page
        self.assertEqual(page.schedule.item(0).text(), "09:00 开发")
        page.tasks.item(0).setCheckState(Qt.CheckState.Checked)
        self.until(lambda: "[x] 验证恢复" in self.daily.load()["text"] and page.tasks.isEnabled())
        # Windows denies rename while Python's read handle is open. Finish our own
        # observation before simulating the external editor's atomic replacement.
        page.clock_timer.stop()
        page.reload_timer.stop()
        self.until(lambda: not page.worker)
        path = Path(self.daily.load()["path"])
        replacement = path.with_suffix(".tmp")
        replacement.write_text(self.daily.load()["text"].replace("验证恢复", "Obsidian 修改任务"), encoding="utf-8")
        replacement.replace(path)
        self.until(lambda: "Obsidian 修改任务" in page.tasks.item(0).text())
        with patch("agent_manager.ui.workbench.QDesktopServices.openUrl", return_value=True) as opened:
            page.open_daily()
            self.assertIn("obsidian://open?", opened.call_args[0][0].toString())
        self.assertNotIn("Obsidian 修改任务", str(self.store.tasks()))

    def test_today_to_project_stage_summary_and_related_library(self):
        page = self.window.today_page
        page.open_project(page.continue_projects.item(0))
        project_page = self.window.project_page
        self.until(lambda: bool(project_page.status_text))
        self.assertEqual(self.window.navigation.currentRow(), self.window.PROJECT)
        self.assertIn("规划", project_page.stage_label.text())
        self.assertIn("阶段总结 Prompt", project_page.related_list.item(0).text())
        def choose(dialog):
            dialog.findChild(QComboBox).setCurrentText("验证新阶段")
            listing = dialog.findChildren(QListWidget)[0]
            listing.item(0).setCheckState(Qt.CheckState.Checked)
            return QDialog.DialogCode.Accepted
        with patch("agent_manager.ui.projects.QDialog.exec", choose):
            project_page.edit_stage()
        self.until(lambda: "验证新阶段" in project_page.stage_label.text())
        details = project_context(self.workspace.document(self.project["project_id"], "agent/STATUS.md")["text"])
        self.assertEqual(details["agents"], [self.agent.id])
        def write(dialog):
            editor = dialog.findChild(QTextEdit)
            editor.setPlainText(editor.toPlainText() + "\n完成内容：已验证 GUI 链路。")
            return QDialog.DialogCode.Accepted
        with patch("agent_manager.ui.projects.QDialog.exec", write):
            project_page.write_stage_summary()
        self.until(lambda: "已验证 GUI 链路" in project_page.summary_view.toPlainText())
        self.assertEqual(len(summaries(self.workspace.document(self.project["project_id"], "agent/HANDOFF.md")["text"])), 1)
        project_page.show_project_resources()
        self.until(lambda: self.window.catalog_page.listing.count() == 1)
        self.assertEqual(self.window.catalog_page.selected()["name"], "阶段总结 Prompt")

    def test_project_cards_source_toggle_menu_and_overview_share_original_files(self):
        self.window.navigation.setCurrentRow(self.window.PROJECT)
        page = self.window.project_page
        self.until(lambda: bool(page.status_text))
        self.assertEqual(page.metrics[1].value.text(), "0 / 1")
        self.assertIn("走通工作入口", page.overview.previews["goals"].toPlainText())
        version = page.document_version
        page.overview.toggle.click()
        self.assertEqual(page.overview.stack.currentIndex(), 1)
        self.assertIn("走通工作入口", page.views[0].toPlainText())
        page.overview.toggle.click()
        self.assertEqual(page.overview.stack.currentIndex(), 0)
        self.assertEqual(page.document_version, version)
        with patch("agent_manager.ui.projects.QDesktopServices.openUrl", return_value=True) as opened:
            page.more_actions[page.open_button].trigger()
            self.assertEqual(Path(opened.call_args[0][0].toLocalFile()), Path(self.project["directory"]))
        status_path = Path(self.project["directory"]) / "agent/STATUS.md"
        status_path.write_text(page.status_text.replace("走通工作入口", "外部修改目标"), encoding="utf-8")
        self.until(lambda: "外部修改目标" in page.overview.previews["goals"].toPlainText())
        self.assertIn("外部修改目标", page.views[0].toPlainText())
        self.window.resize(960, 700)
        QTest.qWait(150)
        for control in (page.summary_button, page.log_button, page.more_button):
            self.assertGreaterEqual(control.width(), control.minimumSizeHint().width())
            self.assertTrue(page.hero.rect().contains(control.geometry().bottomRight()))
        self.assertFalse(page.hero.geometry().intersects(page.metrics_row.geometry()))

    def test_library_create_search_favorite_and_external_edit_do_not_store_body_in_sqlite(self):
        self.window.navigation.setCurrentRow(self.window.LIBRARY)
        page = self.window.catalog_page
        self.until(lambda: len(page.items) == 1)
        def fill(dialog):
            dialog.fields["name"].setText("GitHub MCP 说明")
            dialog.kind.setCurrentIndex(dialog.kind.findData("mcp"))
            dialog.flags["tested"].setChecked(True)
            dialog.body.setPlainText("fixture-private-resource-body\n\n## 安装说明\n人工配置")
            return QDialog.DialogCode.Accepted
        with patch.object(CatalogDialog, "exec", fill):
            page.edit_item(None)
        self.until(lambda: len(page.items) == 2)
        page.search.setText("人工配置")
        self.assertEqual(page.listing.count(), 1)
        self.assertEqual(page.selected()["type"], "mcp")
        page.toggle_favorite()
        self.until(lambda: page.selected()["metadata"].get("favorite"))
        item = page.selected()
        Path(item["path"]).write_text(item["text"].replace("人工配置", "外部新配置"), encoding="utf-8")
        page.search.clear()
        self.until(lambda: any("外部新配置" in i["body"] for i in page.items))
        self.assertNotIn("fixture-private-resource-body", str(self.store.tasks()))
        self.assertNotIn(b"fixture-private-resource-body", self.store.path.read_bytes())

    def test_existing_agent_opens_same_safety_registration_and_daily_has_no_backup_controls(self):
        self.window.navigation.setCurrentRow(self.window.AGENT)
        self.window.agent_page.refresh(self.agent.id)
        self.window.agent_page.safety()
        self.assertEqual(self.window.navigation.currentRow(), self.window.SAFETY)
        self.assertEqual(self.window.resource_pages[3].selected().id, self.agent.id)
        self.assertEqual(self.window.safety_tabs.currentIndex(), 0)
        self.assertFalse(self.window.timer.isActive())

    def test_change_notes_location_keeps_old_files_and_engine_resource_filter_and_url_guard(self):
        old = Path(self.daily.load()["path"])
        before = old.read_bytes()
        destination = self.root / "portable-notes"
        destination.mkdir()
        with patch("agent_manager.ui.window.QFileDialog.getExistingDirectory", return_value=str(destination)):
            self.window.change_workbench_location()
        self.until(lambda: not self.window.today_page.worker and not self.window.catalog_page.worker)
        self.assertEqual(work_root(self.store).resolve(), destination.resolve())
        self.assertEqual(old.read_bytes(), before)
        Catalog(destination).save({"name": "适用 Codex", "type": "skill", "agents": ["codex"]}, "说明")
        self.window.catalog_page.agent_filter = self.agent.id
        self.window.navigation.setCurrentRow(self.window.LIBRARY)
        self.until(lambda: self.window.catalog_page.listing.count() == 1)
        from PySide6.QtCore import QUrl
        with patch("agent_manager.ui.workbench.QDesktopServices.openUrl") as opened, patch("agent_manager.ui.workbench.QMessageBox.information"):
            self.window.catalog_page.safe_url(QUrl("file:///C:/not-a-website.exe"))
            opened.assert_not_called()

    def test_closing_window_during_daily_read_does_not_reopen_sqlite(self):
        gate, entered = threading.Event(), threading.Event()
        original = Daily.load
        def delayed(daily):
            entered.set()
            gate.wait(3)
            return original(daily)
        self.until(lambda: not self.window.today_page.worker)
        with patch.object(Daily, "load", delayed):
            self.window.today_page.refresh()
            self.until(entered.is_set)
            self.window.close()
            with patch.object(self.store, "setting", side_effect=AssertionError("Closed daily read must not open SQLite")) as reopened:
                gate.set()
                self.until(lambda: not self.window.today_page.worker)
                reopened.assert_not_called()

    def test_manual_agent_without_directory_does_not_open_current_working_folder(self):
        manual = Resource("未配置的新 Agent", "agent", {})
        self.store.save_resource(manual)
        self.window.agent_page.refresh(manual.id)
        with patch.object(self.window, "open_path") as opened, patch("agent_manager.ui.workbench.QMessageBox.information"):
            self.window.agent_page.open_agent()
            self.window.agent_page.directory()
            opened.assert_not_called()
        self.assertEqual(len(self.store.resources()), 2)

    def test_missing_task_file_keeps_status_logs_and_notes_usable(self):
        tasks = Path(self.project["directory"]) / "agent/TASKS.md"
        tasks.unlink()
        self.window.navigation.setCurrentRow(self.window.PROJECT)
        page = self.window.project_page
        self.until(lambda: 1 in page.document_errors)
        self.assertIn("走通工作入口", page.overview.previews["goals"].toPlainText())
        self.assertIn("无法读取", page.views[1].toPlainText())
        self.assertTrue(page.log_button.isEnabled())
        page.documents.setCurrentIndex(1)
        self.assertFalse(page.edit_button.isEnabled())
        self.assertFalse(tasks.exists())
        self.assertNotIn("读取中", page.stage_label.text())

    def test_empty_workspace_has_explicit_guidance_and_no_active_project_actions(self):
        self.store.set_setting("project_workspace", "")
        self.window.navigation.setCurrentRow(self.window.PROJECT)
        self.until(lambda: not self.window.jobs)
        page = self.window.project_page
        self.assertEqual(page.table.rowCount(), 0)
        self.assertIn("先选择", page.project_name.text())
        for control in (page.new_button, page.resource_button, page.summary_button, page.log_button, page.more_button):
            self.assertFalse(control.isEnabled())
        self.assertIn(str(self.store.root), page.message.text())

    def test_new_project_is_selected_even_after_search_and_keeps_previous_project(self):
        self.window.navigation.setCurrentRow(self.window.PROJECT)
        page = self.window.project_page
        self.until(lambda: bool(page.status_text))
        page.search.setText("不存在的项目")
        def fill(dialog):
            dialog.name.setText("新初始化项目")
            dialog.goal.setPlainText("验证项目能立即找到")
            dialog.entry.setChecked(False)
            return QDialog.DialogCode.Accepted
        with patch("agent_manager.ui.projects.ProjectDialog.exec", fill), patch("agent_manager.ui.projects.QMessageBox.information"):
            page.new_button.click()
            self.until(lambda: page.selected() is not None and page.selected()["name"] == "新初始化项目" and bool(page.status_text))
        self.assertEqual(page.search.text(), "")
        self.assertEqual(len(self.workspace.list_projects(TaskContext())["projects"]), 2)

    def test_scoped_resource_create_inherits_project_and_survives_previous_filters(self):
        self.window.navigation.setCurrentRow(self.window.PROJECT)
        project = self.window.project_page
        self.until(lambda: bool(project.status_text))
        page = self.window.catalog_page
        page.search.setText("找不到")
        page.kind.setCurrentIndex(page.kind.findData("mcp"))
        page.favorite.setChecked(True)
        project.resource_button.click()
        self.until(lambda: page.listing.count() == 1)
        def fill(dialog):
            self.assertEqual(dialog.fields["projects"].text(), self.project["project_id"])
            dialog.fields["name"].setText("项目专属 Skill")
            dialog.kind.setCurrentIndex(dialog.kind.findData("skill"))
            return QDialog.DialogCode.Accepted
        with patch.object(CatalogDialog, "exec", fill):
            page.edit_item(None)
        self.until(lambda: page.listing.count() == 2)
        self.assertEqual(page.selected()["name"], "项目专属 Skill")
        self.assertIn("项目专属 Skill", page.preview.toPlainText())
        entry = next(i for i in page.items if i["name"] == "项目专属 Skill")
        self.assertEqual(entry["metadata"]["projects"], [self.project["project_id"]])
        page.search.setText("找不到")
        page.favorite.setChecked(True)
        page.clear_scope()
        self.assertEqual(page.listing.count(), 2)

    def test_invalid_project_workspace_does_not_hide_today_plan(self):
        self.store.set_setting("project_workspace", str(self.root / "missing-workspace"))
        self.store.set_setting("workbench_root", str(self.daily.files.root))
        self.window.today_page.refresh()
        self.until(lambda: any("工作区需要检查" in error for error in self.window.today_page.project_errors))
        self.assertEqual(self.window.today_page.tasks.item(0).text(), "验证恢复")
        self.assertTrue(self.window.today_page.edit_button.isEnabled())

    def test_failed_folder_open_shows_feedback_and_does_not_record_agent_use(self):
        self.window.agent_page.refresh(self.agent.id)
        with patch("agent_manager.ui.window.QDesktopServices.openUrl", return_value=False), patch("agent_manager.ui.window.QMessageBox.warning") as warning:
            self.window.agent_page.open_agent()
            warning.assert_called_once()
        self.assertFalse(self.store.evidence("agent-used:" + self.agent.id))
        self.store.remove_resource(self.agent.id)
        self.window.agent_page.refresh()
        self.assertTrue(all(not action.isEnabled() for action in self.window.agent_page.actions))

    def test_agent_note_save_opens_saved_markdown_in_library_after_location_change(self):
        location = self.root / "new-notes"
        self.store.set_setting("workbench_root", str(location))
        self.window.navigation.setCurrentRow(self.window.AGENT)
        self.window.agent_page.refresh(self.agent.id)
        def fill(dialog):
            dialog.body.setPlainText("Agent 备注保存验证")
            return QDialog.DialogCode.Accepted
        with patch.object(CatalogDialog, "exec", fill):
            self.window.agent_page.notes()
            self.until(lambda: self.window.navigation.currentRow() == self.window.LIBRARY and self.window.catalog_page.listing.count() == 1)
        self.assertIn("Agent 备注保存验证", self.window.catalog_page.preview.toPlainText())
        self.assertEqual(Catalog(location).scan()["items"][0]["metadata"]["agents"], [self.agent.id])
        self.window.navigation.setCurrentRow(self.window.AGENT)
        def edit(dialog):
            self.assertIn("Agent 备注保存验证", dialog.body.toPlainText())
            dialog.body.setPlainText("Agent 备注更新验证")
            return QDialog.DialogCode.Accepted
        with patch.object(CatalogDialog, "exec", edit):
            self.window.agent_page.notes()
            self.until(lambda: self.window.navigation.currentRow() == self.window.LIBRARY and "更新验证" in self.window.catalog_page.preview.toPlainText())
        self.assertEqual(len(Catalog(location).scan()["items"]), 1)

    def test_daily_refreshes_even_when_atomic_save_notification_is_missed(self):
        page = self.window.today_page
        self.until(lambda: not page.worker)
        page.watcher.blockSignals(True)
        path = Path(self.daily.load()["path"])
        old_count = len(self.store.tasks())
        path.write_text(self.daily.load()["text"].replace("验证恢复", "兜底刷新任务"), encoding="utf-8")
        self.until(lambda: "兜底刷新任务" in page.tasks.item(0).text())
        self.assertEqual(len(self.store.tasks()), old_count)
        page.watcher.blockSignals(False)

    def test_quick_add_and_mouse_checkbox_update_real_file_and_counts(self):
        page = self.window.today_page
        def fill(dialog):
            dialog.title.setText("关键恢复检查")
            dialog.priority.setCurrentText("高")
            dialog.time.setText("10:00")
            return QDialog.DialogCode.Accepted
        with patch("agent_manager.ui.workbench.DailyEntryDialog.exec", fill):
            page.add_entry("今日任务")
        self.until(lambda: len(page.report["tasks"]) == 2)
        self.assertIn("[高] 关键恢复检查 @10:00", self.daily.load()["text"])
        self.assertEqual(page.metrics[1].value.text(), "1 项")
        self.assertIn("关键恢复检查", page.focus_items.item(0).text())
        item = page.tasks.item(1)
        rectangle = page.tasks.visualItemRect(item)
        QTest.mouseClick(page.tasks.viewport(), Qt.MouseButton.LeftButton, pos=rectangle.topLeft() + QPoint(12, 20))
        self.until(lambda: "[x] [高]" in self.daily.load()["text"] and page.tasks.isEnabled())
        self.assertEqual(page.metrics[0].value.text(), "1 / 2")
        self.assertEqual(page.metrics[1].value.text(), "0 项")
        self.assertNotIn("关键恢复检查", page.focus_items.item(0).text())
        self.assertNotIn("关键恢复检查", str(self.store.tasks()))

    def test_focus_timer_is_manual_pauses_and_stops_on_window_close(self):
        page = self.window.today_page
        self.assertFalse(page.focus_timer.isActive())
        with patch("agent_manager.ui.workbench.time.monotonic", return_value=100):
            page.toggle_focus()
        with patch("agent_manager.ui.workbench.time.monotonic", return_value=165):
            page.toggle_focus()
        self.assertEqual(page.focus_elapsed, 65)
        self.assertIn("01:05", page.focus_button.text())
        self.assertFalse(page.focus_timer.isActive())
        page.toggle_focus()
        self.window.close()
        self.assertFalse(page.focus_timer.isActive())

    def test_future_entry_date_navigation_and_completion_preserve_today(self):
        page = self.window.today_page
        future = date.today() + timedelta(days=9)
        original = self.daily.load()["text"]
        def fill(dialog):
            dialog.date.setDate(QDate(future.year, future.month, future.day))
            dialog.title.setText("未来验证")
            return QDialog.DialogCode.Accepted
        with patch("agent_manager.ui.workbench.DailyEntryDialog.exec", fill):
            page.add_entry("今日任务")
        self.until(lambda: page.report and page.report["day"] == future.isoformat())
        self.assertEqual(self.daily.load()["text"], original)
        self.assertIn("当日", page.task_card.title.text())
        page.check_day()
        self.until(lambda: not page.worker)
        self.assertEqual(page.day, future)
        page.tasks.item(0).setCheckState(Qt.CheckState.Checked)
        planned = Daily(self.daily.files.root, future)
        self.until(lambda: "[x] 未来验证" in planned.load()["text"] and page.tasks.isEnabled())
        self.assertEqual(self.daily.load()["text"], original)
        page.today_button.click()
        self.until(lambda: page.report and page.report["day"] == date.today().isoformat())
        self.assertIn("验证恢复", page.tasks.item(0).text())

    def test_late_response_after_date_switch_is_ignored_and_future_browsing_creates_no_file(self):
        page = self.window.today_page
        stale = page.report
        target = date.today() + timedelta(days=12)
        page.set_day(target)
        page.render_daily(stale)
        self.assertIsNone(page.report)
        self.assertFalse(page.tasks.isEnabled())
        self.until(lambda: page.report and page.report["day"] == target.isoformat())
        self.assertEqual(page.report["tasks"], [])
        self.assertFalse(Path(page.report["path"]).exists())

    def test_midnight_follows_today_but_keeps_deliberately_selected_date(self):
        page = self.window.today_page
        yesterday = date.today() - timedelta(days=1)
        page.actual_today = yesterday
        page.set_day(yesterday)
        self.until(lambda: page.report and page.report["day"] == yesterday.isoformat())
        page.check_day()
        self.until(lambda: page.report and page.report["day"] == date.today().isoformat())
        future = date.today() + timedelta(days=4)
        page.set_day(future)
        self.until(lambda: page.report and page.report["day"] == future.isoformat())
        page.actual_today = yesterday
        page.check_day()
        self.until(lambda: not page.worker)
        self.assertEqual(page.day, future)

    def test_agenda_selected_date_add_preserves_existing_plan_and_invalid_range_blocks_open(self):
        future = date.today() + timedelta(days=6)
        planned = Daily(self.daily.files.root, future)
        planned.add_entry("今日任务", "保留原计划", None)
        dialog = QDialog(self.window)
        layout = QVBoxLayout(dialog)
        panel = AgendaPanel(self.window, self.window.today_page, dialog)
        layout.addWidget(panel)
        dialog.show()
        panel.refresh()
        try:
            self.until(lambda: len(panel.rows) == 3)
            row = next(i for i, entry in enumerate(panel.rows) if entry["day"] == future.isoformat())
            panel.table.selectRow(row)
            def fill(entry):
                self.assertEqual(entry.date.date().toPython(), future)
                entry.title.setText("追加未来日程")
                entry.time.setText("16:00")
                return QDialog.DialogCode.Accepted
            with patch("agent_manager.ui.workbench.DailyEntryDialog.exec", fill):
                panel.add("日程")
            self.until(lambda: any(entry["title"] == "16:00 追加未来日程" for entry in panel.rows))
            self.assertIn("保留原计划", planned.load()["text"])
            self.assertNotIn("追加未来日程", self.daily.load()["text"])
            panel.mode.setCurrentIndex(3)
            panel.start.setDate(QDate.currentDate().addDays(7))
            panel.end.setDate(QDate.currentDate())
            self.until(lambda: not panel.worker)
            self.assertIn("开始日期", panel.status.text())
            self.assertFalse(panel.table.isEnabled())
            self.assertFalse(panel.open_button.isEnabled())
        finally:
            panel.stop_updates()
            self.until(lambda: not panel.worker)
            dialog.close()

    def test_agenda_filters_and_external_edit_open_matching_date(self):
        past = date.today() - timedelta(days=3)
        future = date.today() + timedelta(days=5)
        for day in (past, future):
            Daily(self.daily.files.root, day).add_entry("今日任务", "跨日任务", None)
        dialog = QDialog(self.window)
        panel = AgendaPanel(self.window, self.window.today_page, dialog)
        dialog.setLayout(QVBoxLayout())
        dialog.layout().addWidget(panel)
        dialog.show()
        panel.refresh()
        try:
            self.until(lambda: len(panel.rows) == 3)
            self.assertNotIn(past.isoformat(), [row["day"] for row in panel.rows])
            panel.mode.setCurrentIndex(1)
            self.until(lambda: len(panel.rows) == 3 and any(row["day"] == past.isoformat() for row in panel.rows))
            self.assertTrue(all(row["kind"] == "任务" for row in panel.rows))
            planned = Daily(self.daily.files.root, future)
            report = planned.load()
            planned.save(report["text"].replace("跨日任务", "Obsidian 改未来"), report["original"])
            self.until(lambda: any(row["title"] == "Obsidian 改未来" for row in panel.rows))
            row = next(i for i, entry in enumerate(panel.rows) if entry["day"] == future.isoformat())
            panel.table.selectRow(row)
            panel.open_button.click()
            self.until(lambda: self.window.today_page.report and self.window.today_page.report["day"] == future.isoformat())
            self.assertIn("Obsidian 改未来", self.window.today_page.tasks.item(0).text())
        finally:
            panel.stop_updates()
            self.until(lambda: not panel.worker)
            dialog.close()

    def test_small_window_keeps_project_and_tasks_in_view_without_header_clipping(self):
        page = self.window.today_page
        self.window.resize(960, 700)
        QTest.qWait(150)
        self.assertEqual(page.columns_layout.getItemPosition(page.columns_layout.indexOf(page.columns[1]))[:2], (0, 1))
        self.assertGreaterEqual(page.edit_button.width(), page.edit_button.minimumSizeHint().width())
        self.assertGreaterEqual(page.focus_button.width(), page.focus_button.minimumSizeHint().width())
        self.assertTrue(page.task_card.isVisible())
        self.assertTrue(page.project_card.isVisible())
