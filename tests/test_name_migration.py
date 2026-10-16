"""One-shot migration retains records and rejects conflicts without runtime fallbacks."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from selfManger.project_workspaces import ProjectWorkspace
from selfManger.runtime import TaskContext
from selfManger.storage import Store

spec = importlib.util.spec_from_file_location("name_migration", Path(__file__).resolve().parents[1] / "scripts/migrate_legacy_data.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class FakeKeyring:
    def __init__(self):
        self.values = {(migration.OLD_SERVICE, "common:backup"): "fixture-secret-only"}

    def get_credential(self, service, identity):
        return SimpleNamespace(username="common:backup") if (service, "common:backup") in self.values else None

    def get_password(self, service, identity):
        return self.values.get((service, identity))

    def set_password(self, service, identity, value):
        self.values[service, identity] = value

    def delete_password(self, service, identity):
        del self.values[service, identity]


class NameMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = self.root / "old"
        self.new = self.root / "selfManger"
        self.snapshot = self.root / "snapshot"
        self.store = Store(self.old)
        self.store.set_setting("workbench_root", str(self.old / "notes"))
        self.store.add_planned_task({"title": "preserve user task", "kind": "task"})
        self.workspace = ProjectWorkspace(self.root / "workspace")
        self.workspace.initialize(TaskContext())
        item = self.workspace.create("fixture", "goal", False, TaskContext(), "fixture")
        self.project = Path(item["directory"])
        (self.project / ".selfManger").rename(self.project / ".xiaonian")
        self.rules = self.project / "AGENTS.md"
        self.rules.write_text("# user rules\nSee .xiaonian/config.json\n", encoding="utf-8")
        self.tasks = self.project / "agent/TASKS-AI.md"
        self.tasks.write_text("user notes\n<!-- agent-manager:tasks:start -->\nkeep row\n<!-- agent-manager:tasks:end -->\n", encoding="utf-8")
        self.keyring = FakeKeyring()

    def test_migrates_once_with_verified_snapshot_and_no_legacy_credentials(self):
        result = migration.migrate(self.old, self.new, self.snapshot, workspace=self.workspace.root, keyring=self.keyring, apply=True)
        self.assertFalse(self.old.exists())
        self.assertTrue((self.new / "manager.sqlite3").is_file())
        self.assertEqual(result["counts"]["planned_tasks"], 1)
        self.assertEqual(result["credential_count"], 1)
        self.assertNotIn((migration.OLD_SERVICE, "common:backup"), self.keyring.values)
        self.assertEqual(self.keyring.values[migration.NEW_SERVICE, "common:backup"], "fixture-secret-only")
        self.assertTrue((self.snapshot / "application-data/manager.sqlite3").is_file())
        self.assertTrue((self.project / ".selfManger/config.json").is_file())
        self.assertFalse((self.project / ".xiaonian").exists())
        self.assertTrue(self.rules.read_text(encoding="utf-8").startswith("# user rules"))
        self.assertIn("selfManger:tasks:start", self.tasks.read_text(encoding="utf-8"))
        moved = Store(self.new)
        self.assertEqual(moved.setting("workbench_root"), str(self.new / "notes"))
        self.assertEqual(moved.planned_tasks()[0]["title"], "preserve user task")
        with self.assertRaises(RuntimeError):
            migration.migrate(self.old, self.new, self.root / "second-snapshot", apply=True)

    def test_conflict_stops_before_moving_data_or_writing_snapshot(self):
        (self.project / ".selfManger").mkdir()
        with self.assertRaises(RuntimeError):
            migration.migrate(self.old, self.new, self.snapshot, workspace=self.workspace.root, apply=True)
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())
        self.assertFalse(self.snapshot.exists())

    def test_note_directory_migration_preserves_original_bytes(self):
        old = self.root / "notes-old"
        old.mkdir()
        (old / "user.md").write_bytes(b"user text\r\nkeep original bytes\r\n")
        new = self.root / "notes-new"
        backup = self.root / "notes-snapshot"
        result = migration.migrate_directory(old, new, backup, apply=True)
        self.assertFalse(old.exists())
        self.assertEqual((new / "user.md").read_bytes(), (backup / "user.md").read_bytes())
        self.assertTrue(result["snapshot_verified"])

    def test_failure_rolls_back_files_database_and_data_directory(self):
        original = self.rules.read_bytes()
        rename = Path.rename
        def fail_config(path, target):
            if path.name == ".xiaonian":
                raise OSError("fixture rename failure")
            return rename(path, target)
        with patch.object(Path, "rename", fail_config), self.assertRaises(OSError):
            migration.migrate(self.old, self.new, self.snapshot, workspace=self.workspace.root, keyring=self.keyring, apply=True)
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())
        self.assertEqual(self.rules.read_bytes(), original)
        self.assertIn((migration.OLD_SERVICE, "common:backup"), self.keyring.values)
        self.assertEqual(Store(self.old).setting("workbench_root"), str(self.old / "notes"))


if __name__ == "__main__":
    unittest.main()
