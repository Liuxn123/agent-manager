from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from agent_manager import archives
from agent_manager.application import ApplicationService
from agent_manager.domain import Resource, UserError
from agent_manager.maintenance import backup_health, is_due
from agent_manager.security import SecretStore
from agent_manager.storage import Store, now
from agent_manager.runtime import TaskContext
from agent_manager.adapters.server import ssh_command, existing_connection
from agent_manager.ui.search import search_local


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.usb = self.root / "usb-A/AgentManager"
        self.usb.mkdir(parents=True)
        (self.usb / "portable.json").write_text('{}')
        self.store = Store(self.usb / "data")
        self.service = ApplicationService(self.store)
        self.source = self.root / "project"
        self.source.mkdir()
        (self.source / "成果.md").write_text("fixture-work", encoding="utf-8")
        self.resource = Resource("我的项目", "project", {"path": str(self.source), "automatic_backup": True})
        self.store.save_resource(self.resource)
        self.context = TaskContext()
        self.password = "fixture-archive-password"

    def test_portable_paths_and_data_follow_a_changed_drive(self):
        local = self.usb / "projects/local"
        local.mkdir(parents=True)
        self.resource.options["path"] = str(local)
        self.store.save_resource(self.resource)
        self.store.set_setting("backup_root", str(self.usb / "backups"))
        self.store.save_evidence(str(self.usb / "backups/example.amb"), {"source": str(local)})
        other = self.root / "usb-B/AgentManager"
        shutil.copytree(self.usb, other)
        moved = Store(other / "data")
        self.assertEqual(Path(moved.resources()[0].options["path"]), other / "projects/local")
        self.assertEqual(Path(moved.setting("backup_root")), other / "backups")
        self.assertEqual(Path(moved.evidence(str(other / "backups/example.amb"))["source"]), other / "projects/local")

    def test_encrypted_portable_credentials_move_and_reject_wrong_master_password(self):
        vault = self.service.secrets
        vault.unlock("fixture-master-password")
        vault.save(self.resource.id + ":backup", self.password)
        raw = vault.vault.read_bytes()
        self.assertNotIn(self.password.encode(), raw)
        moved_root = self.root / "another-computer/AgentManager"
        shutil.copytree(self.usb, moved_root)
        moved = SecretStore(moved_root)
        with self.assertRaises(UserError):
            moved.unlock("incorrect-master-password")
        self.assertIsNone(moved.get(self.resource.id + ":backup"))
        moved.unlock("fixture-master-password")
        self.assertEqual(moved.get(self.resource.id + ":backup"), self.password)
        moved.lock()
        self.assertIsNone(moved.get(self.resource.id + ":backup"))

    def test_backup_verification_and_actual_rehearsal_survive_restart(self):
        result = self.service.backup(self.resource, self.context, self.password)
        self.assertTrue(result["verified"])
        self.assertEqual(backup_health(self.store, self.resource, self.service.backup_root())["state"], "已校验")
        report = self.service.rehearse(self.resource, Path(result["archive"]), self.password, self.context)
        self.assertEqual(report["identical_files"], 1)
        self.assertEqual((self.source / "成果.md").read_text(), "fixture-work")
        restarted = Store(self.store.root)
        self.assertEqual(backup_health(restarted, self.resource, self.service.backup_root())["state"], "已演练")
        source = Path(result["archive"])
        with source.open("ab") as handle:
            handle.write(b"corrupted")
        self.assertFalse(backup_health(restarted, self.resource, self.service.backup_root())["verified"])

    def test_parallel_credential_saves_keep_every_resource(self):
        vault = self.service.secrets
        vault.unlock("fixture-master-password")
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda key: vault.save(key, "fixture-" + key), ["a", "b", "c"]))
        vault.lock()
        vault.unlock("fixture-master-password")
        self.assertEqual([vault.get(key) for key in ["a", "b", "c"]], ["fixture-a", "fixture-b", "fixture-c"])

    def test_automatic_backup_cadence_and_retention_preserve_originals(self):
        self.service.secrets.unlock("fixture-master-password")
        self.service.secrets.save(self.resource.id + ":backup", self.password)
        first = self.service.backup(self.resource, self.context, self.password)
        second = self.service.backup(self.resource, self.context, self.password)
        # Make ordering explicit even on a fast filesystem / same-second creation.
        for value, stamp in [(first, "2020-01-01T00:00:00+00:00"), (second, "2021-01-01T00:00:00+00:00")]:
            sidecar = Path(value["archive"]).with_suffix(".json")
            metadata = json.loads(sidecar.read_text())
            metadata["created_at"] = stamp
            sidecar.write_text(json.dumps(metadata))
        self.store.set_setting("backup_keep", 2)
        latest = self.service.automatic_backup(self.resource, self.context)
        self.assertFalse(Path(first["archive"]).exists())
        self.assertTrue(Path(second["archive"]).exists())
        self.assertTrue(Path(latest["archive"]).exists())
        self.assertTrue((self.source / "成果.md").exists())
        self.assertFalse(is_due(self.store, self.resource))
        self.assertTrue(is_due(self.store, self.resource, datetime.now(timezone.utc) + timedelta(days=2)))

    def test_automatic_backup_skips_locked_credentials_and_running_agents(self):
        self.assertTrue(self.service.automatic_backup(self.resource, self.context)["skipped"])
        self.resource.kind = "agent"
        with patch.object(self.service.registry.get(self.resource), "external_pids", return_value=[123]):
            self.assertTrue(self.service.automatic_backup(self.resource, self.context)["skipped"])
        self.assertFalse(self.service.backup_root().exists())

    def test_active_wal_database_backup_includes_committed_rows_without_wal_files(self):
        records = self.root / "records"
        records.mkdir()
        connection = sqlite3.connect(records / "work.db")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE messages (body TEXT)")
        connection.execute("INSERT INTO messages VALUES ('fixture-new-message')")
        connection.commit()
        self.assertTrue((records / "work.db-wal").exists())
        resource = Resource("运行中的数据库", "agent", {"portable_bundle": True, "record_paths": [str(records)]})
        result = self.service.backup(resource, self.context, self.password)
        target = self.root / "restored"
        plan = self.service.plan_restore(resource, self.context, source=result["archive"], target=str(target), password=self.password)
        self.service.restore(resource, plan, self.context, self.password)
        with closing(sqlite3.connect(target / "records/work.db")) as db:
            self.assertEqual(db.execute("SELECT body FROM messages").fetchone()[0], "fixture-new-message")
        self.assertFalse((target / "records/work.db-wal").exists())

    def test_read_only_search_does_not_return_credentials_or_modify_records(self):
        records = self.root / "records"
        records.mkdir()
        (records / "chat.jsonl").write_text(json.dumps({"payload": {"role": "user", "content": "fixture-project-name"}}))
        (records / "auth.json").write_text('{"token":"fixture-project-name-secret"}')
        resource = Resource("测试 Agent", "agent", {"record_paths": [str(records)], "portable_bundle": True})
        result = search_local([resource], "fixture-project-name", self.context)
        self.assertEqual(len(result["matches"]), 1)
        self.assertNotIn("secret", str(result))
        self.assertEqual(len(self.store.tasks()), 0)

    def test_ssh_login_and_known_hosts_are_explicit_and_environment_import_excludes_passwords(self):
        known = self.root / "known_hosts"
        known.write_text("fixture-host-key")
        options = {"host": "hermes-server", "user": "root", "known_hosts": str(known)}
        command = ssh_command(Resource("server", "hermes_server", options), {"action": "observe"})
        self.assertIn("-l", command)
        self.assertIn("root", command)
        self.assertIn("UserKnownHostsFile=" + str(known), command)
        with patch.dict("os.environ", {"HERMES_SERVER_USER": "root", "HERMES_SERVER_PASSWORD": "fixture-private"}, clear=True), patch("pathlib.Path.home", return_value=self.root):
            imported = existing_connection()
        self.assertEqual(imported["user"], "root")
        self.assertNotIn("fixture-private", str(imported))


if __name__ == "__main__":
    unittest.main()
