import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QImage, QTextDocument
from PySide6.QtWidgets import QApplication

from selfManger.domain import UserError
from selfManger.knowledge import KnowledgeIndex
from selfManger.storage import Store
from selfManger.ui.knowledge import KnowledgePage
from selfManger.ui.theme import setup_theme


def isolated_directory(test):
    # Use inherited ACLs; mode 0700 temp directories can deny restricted Windows tokens.
    parent = Path(tempfile.gettempdir()).resolve()
    root = parent / ("knowledge-check-" + uuid4().hex)
    root.mkdir()
    def cleanup():
        resolved = root.resolve()
        if resolved.parent != parent or not resolved.name.startswith("knowledge-check-"):
            raise RuntimeError("Unexpected test cleanup path")
        shutil.rmtree(resolved)
    test.addCleanup(cleanup)
    return root


class KnowledgeIndexTests(unittest.TestCase):
    def setUp(self):
        self.root = isolated_directory(self)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.index = KnowledgeIndex(Store(self.root / "data"), self.vault)

    def test_note_edit_preserves_encoding_and_rejects_external_changes(self):
        note = self.vault / "note.md"
        note.write_bytes(b"\xef\xbb\xbf# Original\r\n\r\nBody\r\n")
        expected = note.read_text(encoding="utf-8-sig")
        self.index.save_note("note.md", expected, "# Updated\n\nNew body\n")
        self.assertEqual(note.read_bytes(), b"\xef\xbb\xbf# Updated\r\n\r\nNew body\r\n")
        note.write_text("Changed elsewhere", encoding="utf-8")
        with self.assertRaises(UserError):
            self.index.save_note("note.md", expected, "Overwrite")
        self.assertEqual(note.read_text(), "Changed elsewhere")
        self.assertFalse(list(self.vault.glob(".*.tmp")))

    def test_broken_external_reference_is_unique_and_still_taggable(self):
        source = self.root / "model.bin"
        source.write_bytes(b"model")
        self.index.add_file(source, copy=False)
        entry = self.index.scan()["items"][0]
        source.unlink()
        self.index.update_metadata(entry["relative"], tags=["model"], favorite=True)
        (self.vault / "model.bin.kref").unlink()
        report = self.index.scan()
        self.assertEqual(len(report["items"]), 1)
        self.assertTrue(report["items"][0]["missing"])
        self.assertEqual(report["items"][0]["tags"], ["model"])
        self.assertTrue(report["items"][0]["favorite"])

    def test_reference_can_be_classified_without_moving_original(self):
        source = self.root / "original.txt"
        source.write_text("Original stays here", encoding="utf-8")
        self.index.create_folder("resources")
        self.index.add_file(source, copy=False)
        entry = self.index.scan()["items"][0]
        self.index.update_metadata(entry["relative"], tags=["reference"])
        self.index.move_file("original.txt.kref", "resources")
        self.assertEqual(source.read_text(), "Original stays here")
        report = self.index.scan()
        self.assertEqual(len(report["items"]), 1)
        self.assertEqual(report["items"][0]["folder"], "resources")
        self.assertEqual(report["items"][0]["tags"], ["reference"])


class LocalWindow(QObject):
    task_completed = Signal(str, str)

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.submissions = 0

    def submit(self, _resource, _title, operation, callback, **_options):
        self.submissions += 1
        callback(operation(None))
        return "local-check"


class KnowledgeBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        setup_theme(cls.app)

    def setUp(self):
        root = isolated_directory(self)
        self.vault = root / "vault"
        (self.vault / "docs" / "nested").mkdir(parents=True)
        (self.vault / "docs" / "code.py").write_text("print('example')", encoding="utf-8")
        (self.vault / "docs" / "nested" / "note.md").write_text("# Example\n\nDistinctive nested content", encoding="utf-8")
        (self.vault / "link.url").write_text("[InternetShortcut]\nURL=https://example.com", encoding="utf-8")
        picture = QImage(80, 40, QImage.Format.Format_RGB32)
        picture.fill(0x2675F5)
        picture.save(str(self.vault / "picture.png"))
        store = Store(root / "data")
        store.set_setting("obsidian_vault_root", str(self.vault))
        self.window = LocalWindow(store)
        self.page = KnowledgePage(self.window)
        self.addCleanup(self.page.close)
        self.assertEqual(self.window.submissions, 0)
        self.page.resize(1380, 860)
        self.page.show()
        self.page.refresh()
        self.app.processEvents()

    def select(self, relative):
        row = next(i for i, entry in enumerate(self.page.shown) if entry["relative"] == relative)
        self.page.file_list.setCurrentCell(row, 0)
        self.page.file_list.selectRow(row)
        self.page.cards.setCurrentRow(row)
        self.app.processEvents()

    def test_folder_browsing_search_and_clearing_filters(self):
        self.assertEqual(self.page.file_list.columnCount(), 4)
        self.assertLessEqual(self.page.type_filter.geometry().right(), self.page.sort_choice.geometry().left())
        self.assertLess(self.page.folder_list.columnWidth(1), self.page.folder_list.columnWidth(0))
        self.page.folder_list.setCurrentItem(self.page.tree_items["docs"])
        self.assertEqual({entry["relative"] for entry in self.page.shown}, {"docs/nested/note.md", "docs/code.py"})
        self.page.search.setText("Distinctive")
        self.page.render_items()
        self.assertEqual([entry["relative"] for entry in self.page.shown], ["docs/nested/note.md"])
        self.page.clear_filters()
        self.assertEqual(len(self.page.shown), 4)
        self.assertFalse(self.page.directory_mode)

    def test_favorite_and_view_switch_keep_selected_resource_without_rescan(self):
        self.select("docs/code.py")
        submissions = self.window.submissions
        self.page.toggle_favorite()
        self.assertEqual(self.window.submissions, submissions)
        self.assertEqual(self.page.selected()["relative"], "docs/code.py")
        self.assertTrue(self.page.selected()["favorite"])
        self.page.toggle_view()
        self.assertEqual(self.page.selected()["relative"], "docs/code.py")
        self.page.toggle_view()
        self.assertEqual(self.page.selected()["relative"], "docs/code.py")

    def test_image_preview_has_decoded_image_and_note_actions_are_specific(self):
        self.select("picture.png")
        resource = self.page.preview.document().resource(QTextDocument.ResourceType.ImageResource, QUrl("knowledge:image"))
        self.assertIsInstance(resource, QImage)
        self.assertFalse(resource.isNull())
        self.assertFalse(self.page.note_button.isEnabled())
        self.select("docs/nested/note.md")
        self.assertTrue(self.page.note_button.isEnabled())
        self.assertIn("Distinctive nested content", self.page.preview.toPlainText())

    def test_pagination_and_search_cover_files_beyond_current_page(self):
        for number in range(205):
            (self.vault / f"file{number:03}.txt").write_text("text", encoding="utf-8")
        self.page.preferred_identity = ""
        self.page.refresh()
        self.assertEqual(self.page.file_list.rowCount(), 200)
        self.assertTrue(self.page.next_button.isEnabled())
        self.page.change_page(1)
        self.assertEqual(self.page.file_list.rowCount(), 9)
        self.assertTrue(self.page.previous_button.isEnabled())
        self.page.search.setText("file204.txt")
        self.page.render_items()
        self.assertEqual(self.page.file_list.rowCount(), 1)
        self.assertEqual(self.page.selected()["relative"], "file204.txt")

    def test_creating_under_project_and_agent_scope_keeps_new_note_visible(self):
        self.page.set_scope(project="P-example", agent="agent-example")
        self.assertFalse(self.page.shown)
        path = self.page.index.add_note("associated", "new note", "")
        self.page.associate_created(path)
        self.page.refresh()
        self.assertEqual([entry["relative"] for entry in self.page.shown], ["associated.md"])
        self.assertEqual(self.page.selected()["projects"], ["P-example"])
        self.assertEqual(self.page.selected()["agents"], ["agent-example"])


if __name__ == "__main__":
    unittest.main()
