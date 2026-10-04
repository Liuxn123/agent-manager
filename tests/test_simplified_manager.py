import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QDialog, QTextBrowser
from agent_manager import archives
from agent_manager.domain import Resource, UserError
from agent_manager.runtime import TaskContext
from agent_manager.snapshots import snapshot_file
from agent_manager.storage import Store
from agent_manager.token_usage import parse_usage, read_usage
from agent_manager.ui.projects import ProjectLogView
from agent_manager.ui.window import MainWindow
from agent_manager.work_summaries import append_summary


class SimplifiedManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.context = TaskContext()

    def tearDown(self):
        self.temp.cleanup()

    def test_codex_backup_excludes_runtime_locks_and_restores_complete_jsonl_and_sqlite(self):
        import sqlite3
        home = self.root / "codex"
        (home / "sessions").mkdir(parents=True)
        transcript = b'{"type":"event_msg","payload":{"text":"fixture"}}\n{"incomplete":'
        (home / "sessions/rollout.jsonl").write_bytes(transcript)
        (home / "config.toml").write_text('model="fixture"')
        for name in (".sandbox", ".sandbox-secrets", "thread-writer-locks"):
            (home / name).mkdir()
            (home / name / "unreadable.lock").write_bytes(b"must exclude")
        guard = home / ".codex-provisioning-fixture.guard"
        guard.write_bytes(b"must exclude")
        with closing(sqlite3.connect(home / "state.sqlite")) as db:
            db.execute("CREATE TABLE records (id INTEGER)")
            db.execute("INSERT INTO records VALUES (1)")
            db.commit()
        resource = Resource("Codex", "agent", {"engine": "Codex", "record_paths": [str(home)], "portable_bundle": True})
        original_open = Path.open
        def blocked(path, *args, **kwargs):
            if path == guard or any(part in {".sandbox", ".sandbox-secrets", "thread-writer-locks"} for part in path.parts):
                raise PermissionError("fixture locked")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", blocked):
            report = archives.create_archive(resource, self.root / "backup", "fixture-password", self.context)
        self.assertEqual(report["file_count"], 3)
        target = self.root / "restored"
        plan = archives.plan_restore(resource, Path(report["archive"]), target, "fixture-password", self.context)
        archives.apply_restore(plan, "fixture-password", self.context)
        restored = next(target.rglob("rollout.jsonl"))
        self.assertEqual(restored.read_bytes(), transcript.splitlines(keepends=True)[0])
        with closing(sqlite3.connect(next(target.rglob("state.sqlite")))) as db:
            self.assertEqual(db.execute("SELECT id FROM records").fetchall(), [(1,)])
        self.assertFalse(list(target.rglob("*.lock")))

    def test_required_unreadable_file_reports_path_and_does_not_create_archive(self):
        home = self.root / "records"
        home.mkdir()
        config = home / "config.toml"
        config.write_text("fixture")
        original = Path.open
        def fail(path, *args, **kwargs):
            if path == config:
                raise PermissionError()
            return original(path, *args, **kwargs)
        resource = Resource("Codex", "agent", {"engine": "Codex", "record_paths": [str(home)], "portable_bundle": True})
        with patch.object(Path, "open", fail), self.assertRaisesRegex(UserError, "config.toml"):
            archives.create_archive(resource, self.root / "backups", "fixture-password", self.context)
        self.assertFalse(list((self.root / "backups").glob("*.amb")))

    def test_live_log_snapshot_has_fixed_boundary_and_does_not_follow_later_appends(self):
        path = self.root / "rollout.jsonl"
        path.write_bytes(b'{"first":1}\n{"partial":')
        with snapshot_file(path, self.context, append_log=True) as (copy, database):
            with path.open("ab") as handle:
                handle.write(b'2}\n{"later":3}\n')
            self.assertEqual(copy.read_bytes(), b'{"first":1}\n')
            self.assertFalse(database)

    def test_standalone_sqlite_fallback_preserves_data_when_source_cannot_create_shm(self):
        import sqlite3
        path = self.root / "standalone.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE facts (id INTEGER)")
            db.execute("INSERT INTO facts VALUES (42)")
            db.commit()
        connect = sqlite3.connect
        def fail_source(database, *args, **kwargs):
            if database == path.as_uri() + "?mode=ro":
                raise sqlite3.OperationalError("unable to open database file")
            return connect(database, *args, **kwargs)
        with patch("agent_manager.snapshots.sqlite3.connect", fail_source):
            with snapshot_file(path, self.context) as (copy, database):
                self.assertTrue(database)
                with closing(connect(copy)) as db:
                    self.assertEqual(db.execute("SELECT id FROM facts").fetchall(), [(42,)])
        # An existing WAL must never be ignored by the standalone fallback.
        path.with_name(path.name + "-wal").write_bytes(b"fixture log")
        with patch("agent_manager.snapshots.sqlite3.connect", fail_source), self.assertRaisesRegex(UserError, "日志文件"):
            with snapshot_file(path, self.context):
                pass

    def test_two_server_profiles_select_separate_display_paths_with_shared_backup_scope(self):
        store = Store(self.root / "data")
        for name in ["pigzhulin", "gugu"]:
            store.save_resource(Resource(name, "hermes_server", {"home": "/shared/.hermes", "profile_name": name, "profile_home": "/shared/.hermes/profiles/" + name}))
        window = MainWindow(store)
        page = window.resource_pages[1]
        row = next(i for i, r in enumerate(page.rows) if r.name == "gugu")
        page.selector.setCurrentIndex(row)
        self.assertEqual(page.selected().name, "gugu")
        self.assertIn("/profiles/gugu", page.details.toPlainText())
        self.assertIn("共用网关", page.details.toPlainText())
        self.assertEqual(page.selected().options["home"], "/shared/.hermes")
        window.close()

    def test_token_usage_handles_repeated_and_interleaved_cumulative_snapshots(self):
        def event(total, last):
            return json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": total, "total_tokens": total},
                "last_token_usage": {"input_tokens": last, "total_tokens": last}}}}).encode()
        # Main stream 10 -> 15 -> 18; reviewer 4 -> 6. Duplicates charge nothing.
        data = b"\n".join(event(t, l) for t, l in [(10, 10), (10, 10), (4, 4), (15, 5), (6, 2), (18, 3)])
        self.assertEqual(parse_usage(data)[-1], 24)
        self.assertIsNone(parse_usage(b'{"type":"chat","text":"private fixture"}'))

    def test_project_logs_show_latest_entry_and_keep_original_available(self):
        browser = QTextBrowser()
        widget = ProjectLogView(browser)
        text = "# HANDOFF\nTechnical rules\n## 2026-10-01 — old\nold result\n## 2026-10-05 — new\nlatest result\n"
        widget.set_text(text)
        self.assertIn("latest result", browser.toPlainText())
        self.assertNotIn("Technical rules", browser.toPlainText())
        widget.all.setChecked(True)
        self.assertIn("Technical rules", browser.toPlainText())
        widget.deleteLater()

    def test_single_session_password_applies_to_all_resources_without_disk_credentials(self):
        store = Store(self.root / "data")
        window = MainWindow(store)
        a, b = Resource("A", "agent", {}), Resource("B", "vault", {})
        self.assertEqual(window.resolve_password(a, "fixture-password", True), "fixture-password")
        self.assertEqual(window.resolve_password(b, ""), "fixture-password")
        self.assertEqual(window.resolve_password(b, "old-backup-password"), "old-backup-password")
        self.assertNotIn("fixture-password", store.path.read_bytes().decode(errors="ignore"))
        window.close()
        self.assertEqual(window.service.common_password, "")

    def test_work_summary_is_persistent_append_only_user_content(self):
        first = append_summary(self.root, "今天完成备份")
        second = append_summary(self.root, "下一步验证恢复")
        self.assertIn(first.rstrip(), second)
        self.assertEqual((self.root / "work-summary.md").read_text(encoding="utf-8"), second)
        with self.assertRaises(UserError):
            append_summary(self.root, " ")

    def test_wrong_migration_password_never_marks_checks_passed(self):
        source = self.root / "project"
        source.mkdir()
        (source / "file.txt").write_text("fixture")
        resource = Resource("fixture", "project", {"path": str(source)})
        report = archives.create_archive(resource, self.root / "backups", "correct-password", self.context)
        window = MainWindow(Store(self.root / "data"))
        from PySide6.QtTest import QTest
        with patch.object(window, "password_input", return_value=("wrong-password", False)), patch("agent_manager.ui.window.QMessageBox.warning"):
            window.import_backup(report["archive"])
            for _ in range(500):
                if not window.jobs:
                    break
                QTest.qWait(10)
        self.assertFalse(window.jobs)
        self.assertTrue(window.migration_checklist.table.item(1, 1).text().startswith("未通过"))
        self.assertFalse(window.migration_checklist.table.item(4, 1).text().startswith("已恢复"))
        window.close()


if __name__ == "__main__":
    unittest.main()
