"""Filesystem backed knowledge library with a rebuildable SQLite metadata index."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from uuid import uuid4
from pathlib import Path
from urllib.parse import urlsplit

from .domain import UserError
from .knowledge_resources import PortableResources
from .knowledge_bundles import ResourceBundles


def knowledge_root(store):
    """Use the configured Obsidian vault, or infer myself from the workspace setting."""
    configured = str(store.setting("obsidian_vault_root", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    workspace = str(store.setting("project_workspace", "") or "").strip()
    if workspace:
        candidate = Path(workspace).expanduser() / "myself"
        if candidate.is_dir():
            return candidate
    return None


class KnowledgeIndex(ResourceBundles, PortableResources):
    EXCLUDED = {".git", ".obsidian", ".local-secrets", ".trash"}
    MAX_FILES = 20000
    TEXT_EXTENSIONS = {".md", ".txt", ".py", ".json", ".yaml", ".yml", ".csv", ".html", ".htm", ".xml", ".toml", ".ini", ".log"}
    MAX_TEXT_FILE = 100_000
    MAX_TEXT_TOTAL = 5_000_000

    def __init__(self, store, root):
        self.store = store
        self.root = Path(root).expanduser().absolute()
        if self.root.is_symlink() or getattr(self.root, "is_junction", lambda: False)():
            raise UserError("知识库根目录不能是目录联接或符号链接。")
        self.root = self.root.parent.resolve() / self.root.name
        self.root_id = hashlib.sha256(str(self.root.resolve()).casefold().encode()).hexdigest()
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS knowledge_metadata (
                root_id TEXT NOT NULL, relative_path TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]',
                favorite INTEGER NOT NULL DEFAULT 0, projects TEXT NOT NULL DEFAULT '[]',
                agents TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(root_id, relative_path))""")
            db.execute("""CREATE TABLE IF NOT EXISTS knowledge_files (
                root_id TEXT NOT NULL, relative_path TEXT NOT NULL, name TEXT NOT NULL, folder TEXT NOT NULL,
                kind TEXT NOT NULL, size INTEGER NOT NULL, modified_ns INTEGER NOT NULL, missing INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(root_id, relative_path))""")

    def _relative(self, path):
        path = Path(path)
        for parent in (path, *path.parents):
            if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                raise UserError("知识库操作不能经过目录联接或符号链接。")
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self.root.resolve()):
            raise UserError("路径必须位于知识库根目录内。")
        return resolved.relative_to(self.root.resolve()).as_posix()

    def scan(self, context=None):
        if not self.root.is_dir():
            return {"items": [], "folders": [], "errors": ["知识库目录不存在，请在设置中选择 myself。"]}
        items, folders, errors, seen = [], {""}, [], set()
        count, text_budget = 0, self.MAX_TEXT_TOTAL
        stack = [(self.root, 0)]
        with self.store.connect() as db:
            self.reconcile_paths(db)
            while stack:
                folder, depth = stack.pop()
                if context:
                    context.checkpoint()
                try:
                    children = sorted(folder.iterdir(), key=lambda p: p.name.casefold())
                except OSError as exc:
                    errors.append(f"{folder.name}：{exc}")
                    continue
                for path in children:
                    if path.name.startswith(".") or path.name in self.EXCLUDED:
                        continue
                    if (self.root / "02-知识").is_dir():
                        if folder == self.root and path.name != "02-知识":
                            continue
                        if path.name == "AGENTS.md":
                            continue
                    if path.name == "知识库索引" and folder.name == "99-系统":
                        continue
                    try:
                        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                            continue
                        relative = self._relative(path)
                        if path.is_dir():
                            folders.add(relative)
                            if depth < 12:
                                stack.append((path, depth + 1))
                            continue
                        if not path.is_file():
                            continue
                        count += 1
                        if count > self.MAX_FILES:
                            errors.append(f"索引达到 {self.MAX_FILES} 个文件上限；请通过分类目录缩小范围。")
                            stack.clear()
                            break
                        suffix = path.suffix.casefold()
                        record = None
                        if suffix == ".kref":
                            if path.stat().st_size > 64_000:
                                raise UserError("外部引用记录超过 64 KB。")
                            record = json.loads(path.read_text(encoding="utf-8"))
                            if not isinstance(record, dict):
                                raise UserError("外部引用记录格式无效。")
                            target = Path(str(record.get("path", "")))
                            if not target.is_absolute():
                                raise UserError("外部引用路径不是绝对路径。")
                            relative = "external:" + json.dumps(str(target), ensure_ascii=False)
                        stat = target.stat() if record and target.is_file() else None if record else path.stat()
                        kind = "external" if record else "link" if suffix == ".url" else "note" if suffix == ".md" else "file"
                        searchable_path = target if record else path
                        search_text = ""
                        if stat and searchable_path.suffix.casefold() in self.TEXT_EXTENSIONS and stat.st_size <= self.MAX_TEXT_FILE and text_budget:
                            raw = searchable_path.read_bytes()[:text_budget]
                            text_budget -= len(raw)
                            search_text = raw.decode("utf-8-sig", errors="replace")
                        seen.add(relative)
                        db.execute("INSERT OR REPLACE INTO knowledge_files VALUES (?,?,?,?,?,?,?,?)",
                                   (self.root_id, relative, str(record.get("name") or target.name) if record else path.stem if kind in {"link", "note"} else path.name,
                                    Path(self._relative(path)).parent.as_posix(), kind, stat.st_size if stat else 0, stat.st_mtime_ns if stat else 0,
                                    int(record is not None and stat is None)))
                        row = db.execute("SELECT tags, favorite, projects, agents FROM knowledge_metadata WHERE root_id=? AND relative_path=?", (self.root_id, relative)).fetchone()
                        metadata = {"tags": json.loads(row[0]), "favorite": bool(row[1]), "projects": json.loads(row[2]), "agents": json.loads(row[3])} if row else {"tags": [], "favorite": False, "projects": [], "agents": []}
                        target_path = str(target) if record else str(path)
                        items.append({"path": target_path, "relative": relative, "name": str(record.get("name") or target.name) if record else path.stem if kind in {"link", "note"} else path.name,
                                      "filename": path.name, "folder": Path(self._relative(path)).parent.as_posix(), "kind": kind,
                                      "size": stat.st_size if stat else 0, "modified_ns": stat.st_mtime_ns if stat else 0,
                                      "suffix": Path(target_path).suffix.casefold() if record else suffix,
                                      "missing": bool(record is not None and stat is None), "search_text": search_text, **metadata})
                    except (OSError, UserError, ValueError) as exc:
                        errors.append(f"{path.name}：{exc}")
            # External references are index entries, not copies. Keep broken paths visible.
            # Retain previously indexed paths so a moved or unavailable file is reported as broken.
            for row in db.execute("SELECT relative_path,name,folder,kind,size,modified_ns FROM knowledge_files WHERE root_id=?", (self.root_id,)).fetchall():
                if row[0] in seen:
                    continue
                if not row[0].startswith("external:") and any(part.startswith(".") for part in Path(row[0]).parts):
                    continue
                if (self.root / "02-知识").is_dir():
                    if row[0].startswith("external:"):
                        if row[2] != "02-知识" and not row[2].startswith("02-知识/"):
                            continue
                    elif not row[0].startswith("02-知识/"):
                        continue
                meta = db.execute("SELECT tags,favorite,projects,agents FROM knowledge_metadata WHERE root_id=? AND relative_path=?", (self.root_id, row[0])).fetchone()
                missing_path = Path(json.loads(row[0][len("external:"):])) if row[0].startswith("external:") else self.root / row[0]
                items.append({"path": str(missing_path), "relative": row[0], "name": row[1], "filename": Path(row[0]).name,
                    "folder": row[2], "kind": row[3], "size": row[4], "modified_ns": row[5], "suffix": Path(row[0]).suffix.casefold(),
                    "tags": json.loads(meta[0]) if meta else [], "favorite": bool(meta[1]) if meta else False,
                    "projects": json.loads(meta[2]) if meta else [], "agents": json.loads(meta[3]) if meta else [], "missing": True, "search_text": ""})
                seen.add(row[0])
                db.execute("UPDATE knowledge_files SET missing=1 WHERE root_id=? AND relative_path=?", (self.root_id, row[0]))
            rows = db.execute("""SELECT m.relative_path,m.tags,m.favorite,m.projects,m.agents
                FROM knowledge_metadata m JOIN knowledge_files f
                  ON f.root_id=m.root_id AND f.relative_path=m.relative_path
                WHERE m.root_id=? AND m.relative_path LIKE 'external:%'
                  AND (f.folder='02-知识' OR f.folder LIKE '02-知识/%')""", (self.root_id,)).fetchall()
            for row in rows:
                key = row[0]
                if key in seen:
                    continue
                path = Path(json.loads(key[len("external:"):]))
                try:
                    stat = path.stat() if path.is_file() else None
                except OSError:
                    stat = None
                items.append({"path": str(path), "relative": key, "name": path.name, "filename": path.name,
                    "folder": "外部引用", "kind": "file", "size": stat.st_size if stat else 0,
                    "modified_ns": stat.st_mtime_ns if stat else 0, "suffix": path.suffix.casefold(),
                    "tags": json.loads(row[1]), "favorite": bool(row[2]), "projects": json.loads(row[3]),
                    "agents": json.loads(row[4]), "missing": stat is None,
                    "search_text": self.read_search_text(path, stat.st_size if stat else 0, text_budget)})
                text_budget -= len(items[-1]["search_text"].encode("utf-8"))
        self.enrich_resources(items, errors)
        items = self.build_bundles(items, context, errors)
        content_count = len({entry["relative"] for resource in items for entry in (resource["members"] if resource["kind"] == "bundle" else [resource])})
        for entry in items:
            if entry["kind"] == "bundle":
                directory = entry["bundle_record"].get("directory", "")
                if directory and not Path(directory).is_absolute() and directory != entry["folder"]:
                    folders = {folder for folder in folders if folder != directory and not folder.startswith(directory + "/")}
        items.sort(key=lambda item: (not item["favorite"], item["name"].casefold()))
        return {"items": items, "folders": sorted(folders, key=lambda value: (value.count("/"), value.casefold())), "errors": errors, "content_count": content_count}

    def read_search_text(self, path, size, budget):
        if not budget or size > self.MAX_TEXT_FILE or path.suffix.casefold() not in self.TEXT_EXTENSIONS:
            return ""
        try:
            return path.read_bytes()[:budget].decode("utf-8-sig", errors="replace")
        except OSError:
            return ""

    def update_metadata(self, relative, tags=None, favorite=None, projects=None, agents=None):
        if relative.startswith("bundle:"):
            group = getattr(self, "bundle_entries", {}).get(relative)
            if group and not self.bundle_path(group["bundle_record"]["id"]).exists():
                self.change_bundle(group)
            record = self.read_record(relative)
            changes = {key: value for key, value in (("tags", tags), ("favorite", favorite), ("projects", projects), ("agents", agents)) if value is not None}
            for key in ("tags", "projects", "agents"):
                if key in changes:
                    changes[key] = list(dict.fromkeys(str(value).strip() for value in changes[key] if str(value).strip()))
                    if len(changes[key]) > 100 or any(len(value) > 100 for value in changes[key]):
                        raise UserError("标签与关联最多 100 项，每项最多 100 字。")
            self.write_record(relative, {**record, **changes})
            return
        if relative.startswith("external:"):
            path = Path(json.loads(relative[len("external:"):]))
        else:
            path = self.root / relative
            self._relative(path)
        with self.store.connect() as db:
            row = db.execute("SELECT tags,favorite,projects,agents FROM knowledge_metadata WHERE root_id=? AND relative_path=?", (self.root_id, relative)).fetchone()
            indexed = db.execute("SELECT 1 FROM knowledge_files WHERE root_id=? AND relative_path=?", (self.root_id, relative)).fetchone()
            if not path.is_file() and not row and not indexed:
                raise UserError("此资料尚未登记，请先刷新知识库。")
            old = [json.loads(row[0]), bool(row[1]), json.loads(row[2]), json.loads(row[3])] if row else [[], False, [], []]
            record = self.read_record(relative)
            old = [record.get(key, value) for key, value in zip(("tags", "favorite", "projects", "agents"), old)]
            values = [tags if tags is not None else old[0], favorite if favorite is not None else old[1],
                      projects if projects is not None else old[2], agents if agents is not None else old[3]]
            values[0] = list(dict.fromkeys(str(v).strip() for v in values[0] if str(v).strip()))
            if any(len(v) > 100 for v in values[0]) or len(values[0]) > 100:
                raise UserError("标签最多 100 个，每个不超过 100 字。")
            from .storage import now
            self.write_record(relative, {**record, **dict(zip(("tags", "favorite", "projects", "agents"), values))})
            db.execute("INSERT OR REPLACE INTO knowledge_metadata VALUES (?,?,?,?,?,?,?)",
                       (self.root_id, relative, json.dumps(values[0], ensure_ascii=False), int(values[1]),
                        json.dumps(values[2], ensure_ascii=False), json.dumps(values[3], ensure_ascii=False), now()))

    def save_note(self, relative, expected_text, body):
        """Save a bounded local Markdown note, rejecting concurrent edits."""
        if relative.startswith("external:"):
            raise UserError("外部文件请使用原编辑器修改。")
        path = self.root / relative
        self._relative(path)
        if path.suffix.casefold() != ".md" or not path.is_file() or path.stat().st_size > 1_000_000:
            raise UserError("仅支持编辑不超过 1 MB 的知识库 Markdown 笔记。")
        with path.open("rb") as incoming:
            original = incoming.read(1_000_001)
        if len(original) > 1_000_000:
            raise UserError("笔记内容已超过 1 MB，请使用外部编辑器打开。")
        if original.decode("utf-8-sig").replace("\r\n", "\n") != expected_text:
            raise UserError("笔记已被其他程序修改，请刷新后重新编辑。")
        if "\r\n" in original.decode("utf-8-sig"):
            body = body.replace("\r\n", "\n").replace("\n", "\r\n")
        output = body.encode("utf-8")
        if original.startswith(b"\xef\xbb\xbf"):
            output = b"\xef\xbb\xbf" + output
        if len(output) > 1_000_000:
            raise UserError("笔记正文不能超过 1 MB。")
        temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(output)
            with path.open("rb") as current:
                unchanged = current.read(1_000_001) == original
            if not unchanged:
                raise UserError("笔记保存前发生了外部修改，请刷新后重试。")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def create_folder(self, relative):
        target = self.root / relative
        self._relative(target)
        name = target.name
        if not name or name.startswith(".") or any(c in name for c in '\\:*?"<>|') or name in self.EXCLUDED:
            raise UserError("请填写有效的文件夹名称。")
        if target.exists():
            raise UserError("同名文件夹已存在。")
        target.mkdir(parents=True)

    def add_file(self, source, folder="", copy=False, context=None):
        source = Path(source).expanduser()
        if any(parent.is_symlink() or getattr(parent, "is_junction", lambda: False)() for parent in (source, *source.parents)):
            raise UserError("请选择普通文件，不能使用符号链接。")
        source = source.resolve(strict=True)
        if not source.is_file():
            raise UserError("请选择文件。")
        destination_folder = self.root / folder
        self._relative(destination_folder)
        if not destination_folder.is_dir():
            raise UserError("请先选择现有知识库文件夹。")
        target = destination_folder / source.name
        self._relative(target)
        if target.exists():
            raise UserError("目标目录存在同名文件，请先改名后再添加。")
        if copy:
            try:
                with source.open("rb") as incoming, target.open("xb") as outgoing:
                    while True:
                        if context:
                            context.checkpoint()
                        chunk = incoming.read(1024 * 1024)
                        if not chunk:
                            break
                        outgoing.write(chunk)
                shutil.copystat(source, target)
            except FileExistsError as exc:
                raise UserError("目标目录存在同名文件，请先改名后再添加。") from exc
            except Exception:
                target.unlink(missing_ok=True)
                raise
            return target
        relative = "external:" + json.dumps(str(source), ensure_ascii=False)
        record_path = destination_folder / (source.name + ".kref")
        self._relative(record_path)
        if record_path.exists():
            raise UserError("目标目录已存在此文件的外部引用记录。")
        with record_path.open("x", encoding="utf-8", newline="\n") as output:
            json.dump({"version": 1, "name": source.name, "path": str(source)}, output, ensure_ascii=False, indent=2)
            output.write("\n")
        from .storage import now
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO knowledge_metadata(root_id,relative_path,updated_at) VALUES (?,?,?)", (self.root_id, relative, now()))
        return source

    def rename_folder(self, relative, new_name):
        source = self.root / relative
        self._relative(source)
        new_name = new_name.strip()
        if not new_name or any(c in new_name for c in '\\/:*?"<>|') or new_name.startswith("."):
            raise UserError("请填写有效的文件夹名称。")
        target = source.parent / new_name
        self._relative(target)
        if not source.is_dir() or target.exists():
            raise UserError("文件夹不存在或目标名称已被占用。")
        source.rename(target)
        new_relative = self._relative(target)
        with self.store.connect() as db:
            for table in ("knowledge_metadata", "knowledge_files"):
                prefix = relative + "/"
                rows = db.execute(f"SELECT relative_path FROM {table} WHERE root_id=? AND (relative_path=? OR substr(relative_path,1,?)=?)",
                                  (self.root_id, relative, len(prefix), prefix)).fetchall()
                for row in rows:
                    old = row[0]
                    changed = new_relative + old[len(relative):]
                    if table == "knowledge_metadata":
                        db.execute("UPDATE OR IGNORE knowledge_metadata SET relative_path=? WHERE root_id=? AND relative_path=?", (changed, self.root_id, old))
                    else:
                        db.execute("UPDATE OR IGNORE knowledge_files SET relative_path=?, folder=? WHERE root_id=? AND relative_path=?",
                                   (changed, Path(changed).parent.as_posix(), self.root_id, old))
                    record = self.read_record(old)
                    if record:
                        self.write_record(changed, {**record, "relative": changed})
        self.relocate_bundle_references(relative, new_relative)

    def move_file(self, relative, destination_folder):
        if relative.startswith("external:"):
            raise UserError("外部引用保留在原位置，不能按 Vault 文件移动。")
        source = self.root / relative
        self._relative(source)
        folder = self.root / destination_folder
        self._relative(folder)
        if not source.is_file() or source.is_symlink() or getattr(source, "is_junction", lambda: False)() or not folder.is_dir():
            raise UserError("所选资料或目标目录当前不可用。")
        if source.parent.resolve() == folder.resolve():
            return relative
        target = folder / source.name
        new_relative = self._relative(target)
        if target.exists():
            raise UserError("目标目录存在同名文件，请先改名。")
        moved = False
        record = self.read_record(relative)
        try:
            with self.store.connect() as db:
                source.rename(target)
                moved = True
                db.execute("UPDATE knowledge_metadata SET relative_path=? WHERE root_id=? AND relative_path=?", (new_relative, self.root_id, relative))
                db.execute("UPDATE knowledge_files SET relative_path=?,folder=? WHERE root_id=? AND relative_path=?",
                           (new_relative, Path(new_relative).parent.as_posix(), self.root_id, relative))
                if record:
                    self.write_record(new_relative, {**record, "relative": new_relative})
                self.relocate_bundle_references(relative, new_relative)
        except Exception:
            if moved and target.exists() and not source.exists():
                target.rename(source)
            raise
        return new_relative

    def archive_resource(self, entry):
        """Move local originals to 98-归档 and keep enough metadata to restore them."""
        relative = entry["relative"]
        record = self.read_record(relative)
        has_restore_location = any(record.get(key) for key in ("archive_original", "archive_original_directory", "archive_member_locations"))
        # Older versions only toggled a flag. Move those legacy archived items now;
        # the next click can then restore them using the recorded source location.
        archiving = not bool(entry.get("archived")) or not has_restore_location
        archive_root = self.root / "98-归档"
        self._relative(archive_root)
        archive_root.mkdir(parents=True, exist_ok=True)

        if entry["kind"] == "bundle":
            bundle = entry["bundle_record"]
            directory = bundle.get("directory", "")
            if directory and not Path(directory).is_absolute():
                if directory == ".":
                    raise UserError("不能把知识库根目录作为单个资源归档。")
                if archiving:
                    old_directory = directory
                    target_folder = (Path("98-归档") / directory).parent.as_posix()
                    (self.root / target_folder).mkdir(parents=True, exist_ok=True)
                    self.move_folder(directory, target_folder)
                    self.update_resource(relative, archived=True, archive_original_directory=old_directory)
                else:
                    original = record.get("archive_original_directory", "")
                    if not original:
                        raise UserError("找不到此资料合集的原始目录，无法恢复。")
                    (self.root / Path(original).parent).mkdir(parents=True, exist_ok=True)
                    self.move_folder(directory, Path(original).parent.as_posix())
                    self.update_resource(relative, archived=False, archive_original_directory="")
                return
            locations = record.get("archive_member_locations", {})
            if archiving:
                moved_locations = {}
                for member in entry.get("members", []):
                    if member["relative"].startswith("external:"):
                        continue
                    old = member["relative"]
                    destination = (Path("98-归档") / Path(old).parent).as_posix()
                    (self.root / destination).mkdir(parents=True, exist_ok=True)
                    new = self.move_file(old, destination)
                    self.update_resource(new, archived=True, archive_original=old)
                    moved_locations[new] = old
                self.update_resource(relative, archived=True, archive_member_locations=moved_locations)
            else:
                for current, original in locations.items():
                    destination = Path(original).parent.as_posix()
                    (self.root / destination).mkdir(parents=True, exist_ok=True)
                    new = self.move_file(current, destination)
                    self.update_resource(new, archived=False, archive_original="")
                self.update_resource(relative, archived=False, archive_member_locations={})
            return

        external = relative.startswith("external:")
        physical = ((Path(entry["folder"]) / entry.get("filename", "")).as_posix()
                    if external else relative)
        if external and not entry.get("filename", "").endswith(".kref"):
            raise UserError("外部引用记录文件不可用，请刷新后重试。")
        if archiving:
            destination = (Path("98-归档") / Path(physical).parent).as_posix()
            (self.root / destination).mkdir(parents=True, exist_ok=True)
            moved = self.move_file(physical, destination)
            self.update_resource(relative if external else moved, archived=True, archive_original=physical)
            old_parent = self.root / Path(physical).parent
            topic_root = self.root / "02-知识"
            if old_parent.is_relative_to(topic_root):
                while old_parent != topic_root and old_parent != old_parent.parent:
                    try:
                        old_parent.rmdir()
                    except OSError:
                        break
                    old_parent = old_parent.parent
                    if old_parent.parent == topic_root:
                        break
        else:
            original = record.get("archive_original", "")
            if not original:
                raise UserError("找不到此资源的原始位置，无法恢复。")
            destination = Path(original).parent.as_posix()
            (self.root / destination).mkdir(parents=True, exist_ok=True)
            moved = self.move_file(physical, destination)
            self.update_resource(relative if external else moved, archived=False, archive_original="")

    def move_folder(self, relative, destination_folder):
        source = self.root / relative
        destination = self.root / destination_folder
        self._relative(source)
        self._relative(destination)
        if not source.is_dir() or source.is_symlink() or getattr(source, "is_junction", lambda: False)() or not destination.is_dir():
            raise UserError("所选文件夹或目标目录当前不可用。")
        target = destination / source.name
        new_relative = self._relative(target)
        if target.exists():
            raise UserError("归档目录已存在同名文件夹。")
        source.rename(target)
        try:
            with self.store.connect() as db:
                for table in ("knowledge_metadata", "knowledge_files"):
                    prefix = relative + "/"
                    rows = db.execute(f"SELECT relative_path FROM {table} WHERE root_id=? AND (relative_path=? OR substr(relative_path,1,?)=?)",
                                      (self.root_id, relative, len(prefix), prefix)).fetchall()
                    for row in rows:
                        old = row[0]
                        changed = new_relative + old[len(relative):]
                        if table == "knowledge_metadata":
                            db.execute("UPDATE OR IGNORE knowledge_metadata SET relative_path=? WHERE root_id=? AND relative_path=?", (changed, self.root_id, old))
                        else:
                            db.execute("UPDATE OR IGNORE knowledge_files SET relative_path=?, folder=? WHERE root_id=? AND relative_path=?",
                                       (changed, Path(changed).parent.as_posix(), self.root_id, old))
                        record = self.read_record(old)
                        if record:
                            self.write_record(changed, {**record, "relative": changed})
            self.relocate_bundle_references(relative, new_relative)
        except Exception:
            if target.exists() and not source.exists():
                target.rename(source)
            raise

    def delete_empty_folder(self, relative):
        target = self.root / relative
        self._relative(target)
        if not relative or not target.is_dir():
            raise UserError("请选择一个非根目录文件夹。")
        if any(target.iterdir()):
            raise UserError("文件夹非空；知识库只允许从界面删除空文件夹。")
        target.rmdir()

    def add_url(self, name, url, folder="00-收件箱"):
        name, url = name.strip(), url.strip()
        try:
            parsed = urlsplit(url)
            valid_url = parsed.scheme in {"https", "http"} and bool(parsed.hostname) and not parsed.username and not parsed.password
        except ValueError:
            valid_url = False
        if not name or len(name) > 180 or any(c in name for c in '\\/:*?"<>|') or not valid_url or "\n" in url or "\r" in url:
            raise UserError("请填写名称和有效的 http / https 网址。")
        destination = self.root / folder
        self._relative(destination)
        if not destination.is_dir():
            destination = self.root
        path = destination / (name + ".url")
        self._relative(path)
        if path.exists():
            raise UserError("目标目录存在同名网址记录，请更换名称。")
        with path.open("x", encoding="utf-8", newline="\n") as output:
            output.write("[InternetShortcut]\nURL=" + url + "\n")
        return path

    def add_note(self, name, body, folder="00-收件箱"):
        name = name.strip()
        if not name or len(name) > 180 or any(c in name for c in '\\/:*?"<>|'):
            raise UserError("请填写有效的笔记名称。")
        destination = self.root / folder
        self._relative(destination)
        if not destination.is_dir():
            destination = self.root
        path = destination / (name + ".md")
        self._relative(path)
        if path.exists():
            raise UserError("目标目录存在同名笔记，请更换名称。")
        content = "# " + name + "\n\n" + body
        if len(content.encode("utf-8")) > 1_000_000:
            raise UserError("笔记正文不能超过 1 MB，请使用外部编辑器创建大文件。")
        with path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(content)
        return path
