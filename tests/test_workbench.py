import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from agent_manager.domain import Resource, UserError
from agent_manager.storage import Store
from agent_manager.runtime import TaskContext
from agent_manager.project_workspaces import ProjectWorkspace
from agent_manager.workbench import (Daily, Catalog, MarkdownFiles, work_root, migrate_workbench,
    project_context, set_project_details, append_stage, summaries, stage_template, markdown_uri)
from agent_manager.workbench import task_details


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_daily_plain_bullets_and_checkboxes_preserve_manual_sections_and_reject_stale_edits(self):
        daily = Daily(self.root, date(2026, 10, 5))
        original = "# 2026-10-05\n\n## 日程\n- 09:00 开发\n## 今日任务\n- 测试恢复\n- [x] 阶段总结\n## 工作记录\n人工笔记\n"
        daily.save(original, None)
        report = daily.load()
        self.assertEqual(report["schedule"][0]["text"], "09:00 开发")
        self.assertTrue(report["tasks"][1]["done"])
        daily.toggle(report["tasks"][0]["line"], True, original)
        changed = daily.load()["text"]
        self.assertIn("- [x] 测试恢复", changed)
        self.assertIn("人工笔记", changed)
        with self.assertRaises(UserError):
            daily.save("overwrite", original)
        self.assertEqual(daily.load()["text"], changed)
        self.assertNotEqual(Daily(self.root, date(2026, 10, 6)).relative, daily.relative)

    def test_quick_daily_entries_preserve_notes_validate_input_and_reject_stale_file(self):
        daily = Daily(self.root)
        daily.add_entry("今日任务", "完成恢复验证", None, "高", "10:00")
        report = daily.load()
        self.assertEqual(task_details(report["tasks"][0]["text"]), {"title": "完成恢复验证", "priority": "高", "time": "10:00"})
        saved = report["text"] + "\n## 人工记录\n不覆盖我的备注\n"
        daily.save(saved, report["original"])
        with self.assertRaises(UserError):
            daily.add_entry("今日任务", "过期编辑", report["original"])
        daily.add_entry("日程", "测试 Hermes", saved, time="09:00-10:00")
        changed = daily.load()["text"]
        self.assertIn("09:00–10:00 测试 Hermes", changed)
        self.assertIn("## 人工记录\n不覆盖我的备注", changed)
        self.assertEqual(changed.count("## 今日任务"), 1)
        for clock in ("25:00", "09:61", "明天", "09:00–10:00"):
            with self.assertRaises(UserError):
                daily.add_entry("今日任务", "错误时间", changed, time=clock)
        with self.assertRaises(UserError):
            daily.add_entry("今日任务", "第一行\n## 标题", changed)
        self.assertEqual(daily.load()["text"], changed)
        self.assertEqual(task_details("普通旧任务"), {"title": "普通旧任务", "priority": "", "time": ""})
        no_heading = "# 自定义日期\n\n## 工作记录\n原有记录\n"
        daily.save(no_heading, changed)
        daily.add_entry("今日任务", "补充任务", no_heading)
        self.assertIn("原有记录\n\n## 今日任务\n- [ ] 补充任务", daily.load()["text"])

    def test_read_does_not_create_workspace_and_boundaries_reject_links_and_large_files(self):
        root = self.root / "missing"
        self.assertIsNone(Daily(root).load()["original"])
        self.assertEqual(Catalog(root).scan()["items"], [])
        self.assertFalse(root.exists())
        for relative in ("../keep", "C:/keep", "resources/name:stream"):
            with self.assertRaises(UserError):
                MarkdownFiles(root).read(relative)
        oversized = self.root / "large.md"
        oversized.write_bytes(b"x" * 1_000_001)
        with self.assertRaises(UserError):
            MarkdownFiles(self.root).read("large.md")

    def test_library_frontmatter_unknown_fields_external_edits_and_duplicates(self):
        catalog = Catalog(self.root)
        catalog.save({"name": "阶段总结", "type": "prompt", "tags": ["项目"], "agents": ["codex"], "projects": ["P-2026-001"], "favorite": True, "custom": "keep"}, "正文")
        item = catalog.scan()["items"][0]
        catalog.save({"tested": True}, "新正文", item)
        updated = catalog.scan()["items"][0]
        self.assertEqual(updated["metadata"]["custom"], "keep")
        self.assertTrue(updated["metadata"]["tested"])
        Path(updated["path"]).write_text(updated["text"] + "\nObsidian修改", encoding="utf-8")
        with self.assertRaises(UserError):
            catalog.save({"name": "过时版本"}, "stale", updated)
        self.assertIn("Obsidian修改", catalog.scan()["items"][0]["body"])
        shutil.copyfile(updated["path"], self.root / "资源/duplicate.md")
        self.assertEqual(len(catalog.scan()["errors"]), 1)
        (self.root / "资源/bad.md").write_text("---\ntype: mcp\ntags: bad\n---\nbody", encoding="utf-8")
        self.assertEqual(len(catalog.scan()["errors"]), 2)

    def test_legacy_projects_add_custom_phase_and_refs_without_registry_or_notes_rewrite(self):
        workspace = ProjectWorkspace(self.root / "workspace")
        context = TaskContext()
        workspace.initialize(context)
        project = workspace.create("兼容项目", "验证既有规范", False, context)
        identity = project["project_id"]
        registry = workspace.registry_path.read_bytes()
        status = workspace.document(identity, "agent/STATUS.md")["text"]
        self.assertEqual(project_context(status)["phase"], "规划")
        original = status.replace("project_id:", "# 人工注释\ncustom: 保留\nproject_id:")
        workspace.save_document(identity, "agent/STATUS.md", original, status, context)
        set_project_details(workspace, identity, "实验探索", ["agent-id"], ["prompt-id"], original, context)
        updated = workspace.document(identity, "agent/STATUS.md")["text"]
        self.assertIn("# 人工注释\ncustom: 保留", updated)
        self.assertEqual(project_context(updated)["resources"], ["prompt-id"])
        self.assertEqual(workspace.registry_path.read_bytes(), registry)
        append_stage(workspace, identity, "实验探索", stage_template("实验探索") + "\n验证结果：人工记录", context)
        handoff = workspace.document(identity, "agent/HANDOFF.md")["text"]
        self.assertEqual(len(summaries(handoff)), 1)
        self.assertIn("人工记录", summaries(handoff)[0])
        self.assertFalse(list(Path(project["directory"]).rglob("SUMMARY.md")))
        workspace.move(workspace.plan_move(identity, False, context), "归档", context)
        self.assertEqual(project_context(workspace.document(identity, "agent/STATUS.md")["text"])["phase"], "实验探索")
        with self.assertRaises(UserError):
            append_stage(workspace, identity, "实验探索", stage_template("实验探索"), context)

    def test_additive_migration_preserves_existing_registration_settings_and_portable_paths(self):
        portable = self.root / "usb-A"
        portable.mkdir()
        (portable / "portable.json").write_text("{}")
        store = Store(portable / "data")
        resource = Resource("旧 Codex", "agent", {"engine": "Codex", "record_paths": [str(portable / "records")]})
        store.save_resource(resource)
        store.set_setting("workbench_root", str(portable / "notes"))
        store.set_setting("backup_keep", 17)
        store.save_evidence("legacy", {"valid": True})
        migrate_workbench(store)
        migrate_workbench(store)
        self.assertEqual(store.resources()[0].id, resource.id)
        self.assertEqual([Path(p) for p in store.resources()[0].options["record_paths"]], [(portable / "records").resolve()])
        self.assertEqual(store.setting("backup_keep"), 17)
        self.assertTrue(store.evidence("legacy")["valid"])
        Daily(work_root(store)).save("# fixture", None)
        shutil.copytree(portable, self.root / "usb-B")
        moved = Store(self.root / "usb-B/data")
        self.assertEqual(work_root(moved), (self.root / "usb-B/notes").resolve())
        self.assertEqual(Daily(work_root(moved)).load()["text"], "# fixture")
        self.assertNotIn("# fixture", str(moved.setting("workbench_root")))

    def test_obsidian_opens_real_file_and_does_not_create_vault(self):
        daily = Daily(self.root)
        daily.save(daily.template(), None)
        with self.assertRaises(UserError):
            markdown_uri(daily.load()["path"])
        self.assertFalse((self.root / ".obsidian").exists())
        (self.root / ".obsidian").mkdir()
        self.assertTrue(markdown_uri(daily.load()["path"]).startswith("obsidian://open?"))

    def test_handwritten_resource_keeps_identity_after_edit_and_source_links_are_not_followed(self):
        folder = self.root / "资源"
        folder.mkdir()
        path = folder / "人工 Prompt.md"
        path.write_text("---\ntype: prompt\nname: 人工 Prompt\n---\n真实正文", encoding="utf-8")
        catalog = Catalog(self.root)
        old = catalog.scan()["items"][0]
        catalog.save({"favorite": True}, "编辑后的正文", old)
        self.assertEqual(catalog.scan()["items"][0]["id"], old["id"])
        outside = self.root / "外部.md"
        outside.write_text("外部保留", encoding="utf-8")
        linked = folder / "linked.md"
        try:
            linked.symlink_to(outside)
        except OSError:
            self.skipTest("This host does not permit file symlink creation")
        with self.assertRaises(UserError):
            catalog.files.read("资源/linked.md")
        self.assertEqual(outside.read_text(encoding="utf-8"), "外部保留")

    def test_legacy_phase_paragraph_does_not_turn_into_daily_stage_label(self):
        text = "---\nproject_id: P-2026-001\nstatus: active\n---\n- 当前阶段：" + "原有详细进展说明" * 30 + "\n- 下一步：读取最近验证结果。\n"
        self.assertEqual(project_context(text, "active")["phase"], "进行中")
        self.assertEqual(project_context(text.replace("原有详细进展说明" * 30, "L0 基础了解与概念地图，学习中。后续详细说明保留。"), "active")["phase"], "L0 基础了解与概念地图，学习中")
        self.assertIn("原有详细进展说明", text)
        workspace = ProjectWorkspace(self.root / "workspace")
        context = TaskContext()
        workspace.initialize(context)
        project = workspace.create("原文保护", "保存字段而非覆盖进展", False, context)
        original = workspace.document(project["project_id"], "agent/STATUS.md")["text"]
        paragraph = "原有详细进展说明" * 30
        original_with_details = original.replace("- 当前阶段：待明确。", "- 当前阶段：" + paragraph)
        workspace.save_document(project["project_id"], "agent/STATUS.md", original_with_details, original, context)
        set_project_details(workspace, project["project_id"], "测试", [], [], original_with_details, context)
        updated = workspace.document(project["project_id"], "agent/STATUS.md")["text"]
        self.assertIn(paragraph, updated)
        self.assertEqual(project_context(updated)["phase"], "测试")
        short_progress = "已完成页面整理，目前需要检查迁移兼容性并验证旧项目的阶段记录。"
        self.assertLess(len(short_progress), 60)
        shorter = updated.replace(paragraph, short_progress)
        workspace.save_document(project["project_id"], "agent/STATUS.md", shorter, updated, context)
        set_project_details(workspace, project["project_id"], "收尾", [], [], shorter, context)
        final = workspace.document(project["project_id"], "agent/STATUS.md")["text"]
        self.assertIn(short_progress, final)
        self.assertEqual(project_context(final)["phase"], "收尾")
