from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_manager.domain import UserError
from agent_manager.project_workspaces import ProjectWorkspace, MANAGEMENT_FILES, field
from agent_manager.runtime import TaskContext


class ProjectWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        self.workspace = ProjectWorkspace(self.root)
        self.context = TaskContext()
        self.workspace.initialize(self.context)

    def tearDown(self):
        self.temporary.cleanup()

    def create(self, name="验证项目", entry=False):
        return self.workspace.create(name, "完成可核验交付", entry, self.context)

    def test_create_uses_one_status_task_log_source_and_never_reinitializes_existing(self):
        project = self.create()
        directory = Path(project["directory"])
        self.assertTrue(all((directory / name).is_file() for name in MANAGEMENT_FILES))
        self.assertEqual(field((directory / "agent/STATUS.md").read_text(encoding="utf-8"), "status"), "candidate")
        self.assertFalse((directory / ".git").exists())
        self.assertFalse((directory / "src").exists())
        with self.assertRaises(UserError):
            self.create()
        with self.assertRaises(UserError):
            self.workspace.initialize(self.context)

    def test_archive_and_resume_preserve_business_data_number_and_log_history(self):
        project = self.create()
        directory = Path(project["directory"])
        (directory / "payload.bin").write_bytes(bytes(range(256)))
        (directory / "空目录").mkdir()
        self.workspace.append_log(project["project_id"], "第一次真实记录", self.context)
        plan = self.workspace.plan_move(project["project_id"], False, self.context)
        result = self.workspace.move(plan, "阶段暂停，成果待验收", self.context)
        archived = Path(result["directory"])
        self.assertFalse(directory.exists())
        self.assertEqual((archived / "payload.bin").read_bytes(), bytes(range(256)))
        self.assertTrue((archived / "空目录").is_dir())
        self.assertEqual(field((archived / "agent/STATUS.md").read_text(encoding="utf-8"), "status"), "archived")
        with self.assertRaises(UserError):
            self.workspace.append_log(project["project_id"], "不允许写入归档", self.context)
        resumed = self.workspace.move(self.workspace.plan_move(project["project_id"], True, self.context), "继续下一阶段", self.context)
        self.assertEqual(resumed["project_id"], project["project_id"])
        self.assertEqual(resumed["directory"], project["directory"])
        self.assertEqual(resumed["state"], "active")
        log = (directory / "agent/HANDOFF.md").read_text(encoding="utf-8")
        self.assertIn("第一次真实记录", log)
        self.assertIn("归档", log)
        self.assertIn("重新启用", log)

    def test_changed_project_and_conflicting_destination_prevent_move(self):
        project = self.create()
        plan = self.workspace.plan_move(project["project_id"], False, self.context)
        (Path(project["directory"]) / "new.txt").write_text("new")
        with self.assertRaises(UserError):
            self.workspace.move(plan, "归档", self.context)
        self.assertTrue(Path(project["directory"]).exists())
        Path(plan["target"]).mkdir()
        with self.assertRaises(UserError):
            self.workspace.plan_move(project["project_id"], False, self.context)

    def test_reservation_survives_partial_creation_failure_and_number_is_not_reused(self):
        with patch.object(self.workspace, "_link", side_effect=UserError("injected link failure")):
            with self.assertRaises(UserError):
                self.create("失败项目", True)
        reserved = self.workspace.registry()["issued_ids"]
        self.assertEqual(len(reserved), 1)
        self.assertTrue((self.root / "projects" / (reserved[0] + "-失败项目")).exists())
        with self.assertRaises(UserError):
            self.create("不能盲目重跑")

    def test_indexes_preserve_manual_content_and_edit_rejects_stale_original(self):
        index = self.root / "myself/03-项目/项目总览.md"
        index.write_text(index.read_text(encoding="utf-8") + "\n人工说明必须保留\n", encoding="utf-8")
        project = self.create()
        self.assertIn("人工说明必须保留", index.read_text(encoding="utf-8"))
        original = self.workspace.document(project["project_id"], "agent/STATUS.md")["text"]
        path = Path(project["directory"]) / "agent/STATUS.md"
        path.write_text(original + "\n其他助手的修改\n", encoding="utf-8")
        with self.assertRaises(UserError):
            self.workspace.save_document(project["project_id"], "agent/STATUS.md", original, original, self.context)
        self.assertIn("其他助手的修改", path.read_text(encoding="utf-8"))

    def test_existing_manage_lock_and_path_traversal_are_rejected(self):
        lock = self.root / "agent/.manage.lock"
        lock.write_text("another tool")
        with self.assertRaises(UserError):
            self.create()
        self.assertTrue(lock.exists())
        for path in ("../outside", "projects/name:stream", "/outside"):
            with self.assertRaises(UserError):
                self.workspace.path(path)

    def test_obsidian_link_follows_archive_and_resume_without_copying_project(self):
        project = self.create(entry=True)
        item = self.workspace.project(project["project_id"])
        entry = self.root / item["vault_entry"]
        self.assertEqual(entry.resolve(), Path(project["directory"]).resolve())
        result = self.workspace.move(self.workspace.plan_move(project["project_id"], False, self.context), "归档试验", self.context)
        self.assertEqual(entry.resolve(), Path(result["directory"]).resolve())
        result = self.workspace.move(self.workspace.plan_move(project["project_id"], True, self.context), "恢复试验", self.context)
        self.assertEqual(entry.resolve(), Path(result["directory"]).resolve())

    def test_failed_move_leaves_receipt_and_does_not_report_success(self):
        project = self.create()
        plan = self.workspace.plan_move(project["project_id"], False, self.context)
        with patch.object(self.workspace, "update_indexes", side_effect=OSError("injected index failure")):
            with self.assertRaisesRegex(UserError, "保留目录"):
                self.workspace.move(plan, "归档", self.context)
        self.assertTrue(Path(plan["target"]).exists())
        receipt = next((self.root / "agent/history").glob("*-move.json"))
        self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["state"], "prepared")


if __name__ == "__main__":
    unittest.main()
