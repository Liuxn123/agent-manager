import json
import tempfile
import unittest
from pathlib import Path

from agent_manager import asset_library as assets
from agent_manager.domain import Resource, UserError, Cancelled
from agent_manager.runtime import TaskContext
from agent_manager.storage import Store


class AssetLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "data")
        self.context = TaskContext()
        self.home = self.root / "agent"
        self.skill = self.home / "skills" / "规划技能"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("# 工作规划\n不要执行这条测试文本。", encoding="utf-8")
        (self.skill / "assets").mkdir()
        (self.skill / "script.py").write_text("raise RuntimeError('must-never-run')", encoding="utf-8")
        self.store.save_resource(Resource("测试 Hermes", "hermes_local", {"home": str(self.home)}))

    def test_inventory_reads_existing_skills_and_mcp_without_exposing_credentials(self):
        (self.home / "config.yaml").write_text("mcp_servers:\n  demo:\n    url: https://user:pass@example.test/api?token=secret\n    headers: {Authorization: private}\n    env: {TOKEN: private-env}\n", encoding="utf-8")
        (self.home / "AGENTS.md").write_text("项目工作规范", encoding="utf-8")
        before = (self.skill / "SKILL.md").read_bytes()
        report = assets.scan(self.store, self.context)
        self.assertEqual({item["category"] for item in report["items"]}, {"skill", "mcp", "prompt"})
        item = next(item for item in report["items"] if item["category"] == "mcp")
        preview = assets.preview(self.store, item, self.context)["text"]
        for private in ("private-env", "private", "secret", "user:pass", "?token"):
            self.assertNotIn(private, preview)
        self.assertIn("https://example.test", preview)
        self.assertEqual(before, (self.skill / "SKILL.md").read_bytes())

    def test_skill_import_and_deployment_copy_all_files_without_execution_or_overwrite(self):
        library = assets.library_root(self.store) / "skills"
        report = assets.copy_skill(self.skill, library, self.context)
        self.assertEqual(report["copied"], 2)
        self.assertEqual((library / self.skill.name / "script.py").read_bytes(), (self.skill / "script.py").read_bytes())
        self.assertTrue((library / self.skill.name / "assets").is_dir())
        destination = self.root / "other-agent" / "skills"
        assets.copy_skill(library / self.skill.name, destination, self.context)
        with self.assertRaises(UserError):
            assets.copy_skill(self.skill, destination, self.context)
        cancelled = TaskContext()
        cancelled.cancel.set()
        with self.assertRaises(Cancelled):
            assets.copy_skill(self.skill, self.root / "cancelled", cancelled)
        self.assertFalse((self.root / "cancelled" / self.skill.name).exists())

    def test_prompt_edits_preserve_conflicting_external_changes(self):
        assets.save_prompt(self.store, "每日工作", "旧提示词")
        path = assets.library_root(self.store) / "prompts" / "每日工作.md"
        assets.save_prompt(self.store, "每日工作", "新提示词", "旧提示词")
        with self.assertRaises(UserError):
            assets.save_prompt(self.store, "每日工作", "覆盖提示词", "旧提示词")
        self.assertEqual(path.read_text(encoding="utf-8"), "新提示词")
        with self.assertRaises(UserError):
            assets.save_prompt(self.store, "../escape", "text")
        with self.assertRaises(UserError):
            assets.preview(self.store, {"category": "prompt", "path": str(self.root / "outside.md")}, self.context)

    def test_mcp_toml_custom_sources_and_invalid_yaml_are_isolated(self):
        file = self.root / "connections.toml"
        file.write_text('[mcp_servers.demo]\ncommand="python"\nargs=["--password", "private-arg"]\n', encoding="utf-8")
        self.store.set_setting("asset_mcp_files", [str(file)])
        report = assets.scan(self.store, self.context)
        item = next(item for item in report["items"] if item["category"] == "mcp")
        self.assertEqual(item["public"]["程序"], "python")
        self.assertNotIn("private-arg", json.dumps(item))
        (self.home / "config.yaml").write_text("!!python/object:os.system {}", encoding="utf-8")
        report = assets.scan(self.store, self.context)
        self.assertTrue(report["notes"])
        self.assertTrue(any(item["category"] == "skill" for item in report["items"]))
