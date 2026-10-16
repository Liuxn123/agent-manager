"""Whole folders and multi-file resources without moving or duplicating content."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from .domain import UserError


class ResourceBundles:
    MAX_BUNDLES = 1000

    def bundle_directory(self):
        path = self.root / "99-系统" / "知识库索引" / "资源组"
        self._relative(path)
        return path

    def bundle_path(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identity):
            raise UserError("资源组编号无效。")
        return self.bundle_directory() / (identity + ".json")

    def read_bundles(self, errors):
        directory = self.bundle_directory()
        if not directory.is_dir():
            return []
        result = []
        for path in sorted(directory.glob("*.json"))[:self.MAX_BUNDLES]:
            try:
                self._relative(path)
                if path.stat().st_size > 1_000_000:
                    raise UserError("资源组登记超过 1 MB。")
                value = json.loads(path.read_text(encoding="utf-8"))
                self.validate_bundle(value)
                result.append(value)
            except (OSError, ValueError, UserError) as exc:
                errors.append(path.name + "：" + str(exc))
        return result

    def validate_bundle(self, value):
        if not isinstance(value, dict):
            raise UserError("资源组登记格式无效。")
        self.bundle_path(value.get("id", ""))
        if not isinstance(value.get("title"), str) or not value["title"].strip() or len(value["title"]) > 500:
            raise UserError("资源组名称无效。")
        for key in ("title", "folder", "summary", "directory", "entry"):
            if key in value and (not isinstance(value[key], str) or len(value[key]) > 2000):
                raise UserError("资源组字段无效。")
        if value.get("directory"):
            path = Path(value["directory"])
            if not path.is_absolute():
                path = self.root / path
            self.safe_bundle_folder(path)
        self._relative(self.root / value.get("folder", ""))
        members = value.get("members", []) + value.get("excluded", []) if isinstance(value.get("members", []), list) and isinstance(value.get("excluded", []), list) else None
        if not isinstance(members, list) or len(members) > self.MAX_FILES or any(not isinstance(v, str) or len(v) > 1000 or v.startswith("bundle:") for v in members):
            raise UserError("资源组的内容列表无效。")
        for member in members:
            if not member.startswith("external:"):
                self._relative(self.root / member)

    def safe_bundle_folder(self, path):
        path = Path(path).expanduser().absolute()
        for parent in (path, *path.parents):
            if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
                raise UserError("资料目录不能经过联接或符号链接。")
            if parent.name in self.EXCLUDED | {".ssh", ".aws", ".azure"} or (parent.name.startswith(".") and (parent == path or parent.name not in {".codex", ".agents", ".claude"})):
                raise UserError("不能登记隐藏目录或凭据目录。")
        return path

    def save_bundle(self, value):
        self.validate_bundle(value)
        path = self.bundle_path(value["id"])
        self._relative(path)
        original = path.read_bytes() if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        output = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(output) > 1_000_000:
            raise UserError("资源组登记超过 1 MB。")
        temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(output)
            if (path.read_bytes() if path.exists() else None) != original:
                raise UserError("资源组已被其他程序修改，请刷新后重试。")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return "bundle:" + value["id"]

    def create_bundle(self, title, members=None, directory="", folder="00-收件箱", summary=""):
        if not title.strip():
            raise UserError("请填写资源名称。")
        if directory:
            path = self.safe_bundle_folder(directory)
            if not path.is_dir():
                raise UserError("资料目录不存在。")
            directory = path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else str(path)
            if any(value.get("active", True) and value.get("directory") == directory for value in self.read_bundles([])):
                raise UserError("这个目录已登记为资源，请从已有资源中补充内容。")
        else:
            members = list(dict.fromkeys(members or []))
            if not members:
                raise UserError("请选择至少一份内容。")
        return self.save_bundle({"version": 1, "id": uuid4().hex, "title": title.strip(), "folder": folder,
                                 "summary": summary, "members": members or [], "directory": directory, "entry": "", "active": True})

    def change_bundle(self, entry, **changes):
        value = dict(entry["bundle_record"])
        value.update(changes)
        return self.save_bundle(value)

    def relocate_bundle_references(self, old, new):
        def translated(value):
            return new + value[len(old):] if value == old or value.startswith(old + "/") else value
        for record in self.read_bundles([]):
            updated = dict(record)
            for key in ("folder", "directory", "entry"):
                value = record.get(key, "")
                if not Path(value).is_absolute():
                    updated[key] = translated(value)
            for key in ("members", "excluded"):
                updated[key] = [translated(value) for value in record.get(key, [])]
            if updated != record:
                self.save_bundle(updated)

    def external_bundle_members(self, directory, context, errors):
        """Bounded read of only the explicitly registered folder; never copy its files."""
        root = self.safe_bundle_folder(directory)
        result, budget = [], self._bundle_text_budget
        for current, folders, files in os.walk(root, followlinks=False):
            if context:
                context.checkpoint()
            folders[:] = [name for name in folders if not name.startswith(".") and name not in self.EXCLUDED
                          and not (Path(current) / name).is_symlink() and not getattr(Path(current) / name, "is_junction", lambda: False)()]
            if len(Path(current).relative_to(root).parts) >= 12:
                folders.clear()
            for name in sorted(files):
                path = Path(current) / name
                if name.startswith(".") or name in {"AGENTS.md", "auth.json", "credentials.json", "credentials", "id_rsa", "id_ed25519"} or path.suffix.lower() in {".pem", ".key", ".pfx"} or path.is_symlink():
                    continue
                if self._bundle_files_remaining <= 0:
                    errors.append(root.name + "：目录内容达到索引上限。")
                    return result
                try:
                    stat = path.stat()
                    text = self.read_search_text(path, stat.st_size, budget)
                    budget -= len(text.encode("utf-8"))
                    self._bundle_text_budget = max(0, budget)
                    self._bundle_files_remaining -= 1
                    result.append({"relative": "external:" + json.dumps(str(path), ensure_ascii=False), "path": str(path),
                                   "name": path.stem, "filename": name, "folder": str(path.parent), "kind": "external",
                                   "suffix": path.suffix.lower(), "size": stat.st_size, "modified_ns": stat.st_mtime_ns,
                                   "missing": False, "tags": [], "favorite": False, "projects": [], "agents": [], "search_text": text})
                except OSError as exc:
                    errors.append(name + "：" + str(exc))
        self.enrich_resources(result, errors)
        return result

    def build_bundles(self, items, context, errors):
        self._bundle_text_budget = max(0, self.MAX_TEXT_TOTAL - sum(len(entry.get("search_text", "").encode("utf-8")) for entry in items))
        self._bundle_files_remaining = max(0, self.MAX_FILES - len(items))
        records = self.read_bundles(errors)
        knowledge_root = self.root / "02-知识"
        if knowledge_root.is_dir():
            def in_knowledge(path):
                if not path:
                    return False
                candidate = Path(path)
                if candidate.is_absolute():
                    try:
                        relative = candidate.resolve(strict=False).relative_to(self.root.resolve()).as_posix()
                    except ValueError:
                        return False
                else:
                    relative = candidate.as_posix().removeprefix("./").strip("/")
                return relative == "02-知识" or relative.startswith("02-知识/")

            scoped_records = []
            for record in records:
                if not in_knowledge(record.get("folder", "")):
                    continue
                directory = record.get("directory", "")
                if directory and not in_knowledge(directory):
                    continue
                record = dict(record)
                record["members"] = [key for key in record.get("members", [])
                                     if key.startswith("02-知识/") or key.startswith("external:") and
                                     any(item.get("relative") == key for item in items)]
                scoped_records.append(record)
            records = scoped_records
        registered_dirs = {value.get("directory") for value in records}
        records = [value for value in records if value.get("active", True)]
        # Only actual SKILL.md entrances identify Skill packages, never a folder called Skills.
        for entry in items:
            if entry.get("filename", "").casefold() == "skill.md" and entry["folder"] not in registered_dirs:
                folder = entry["folder"]
                identity = "auto-" + hashlib.sha256(folder.encode()).hexdigest()[:32]
                records.append({"id": identity, "title": Path(folder).name or self.root.name, "folder": Path(folder).parent.as_posix(),
                                "directory": folder, "members": [], "entry": entry["relative"], "active": True})
                registered_dirs.add(folder)
        def is_nested(value, other):
            child, parent = value.get("directory", ""), other.get("directory", "")
            if not child or not parent or child == parent:
                return False
            if Path(child).is_absolute() and Path(parent).is_absolute():
                return Path(child).is_relative_to(Path(parent))
            return child.startswith(parent.rstrip("/") + "/")
        records = [value for value in records if not any(is_nested(value, other) for other in records)]
        by_id = {entry["relative"]: entry for entry in items}
        covered, bundles = set(), []
        records.sort(key=lambda value: (-value.get("directory", "").count("/"), value["id"]))
        for value in records:
            if context:
                context.checkpoint()
            directory = value.get("directory", "")
            try:
                if directory and Path(directory).is_absolute():
                    members = self.external_bundle_members(directory, context, errors)
                elif directory:
                    members = [entry for entry in items if directory == "." or entry["folder"] == directory or entry["folder"].startswith(directory.rstrip("/") + "/")]
                else:
                    members = [by_id[key] for key in value.get("members", []) if key in by_id]
                member_ids = {entry["relative"] for entry in members}
                members.extend(by_id[key] for key in value.get("members", []) if key in by_id and key not in member_ids)
                for key in value.get("members", []):
                    if key in by_id:
                        continue
                    path = Path(json.loads(key[len("external:"):])) if key.startswith("external:") else self.root / key
                    members.append({"relative": key, "path": str(path), "name": path.stem, "filename": path.name,
                                    "folder": path.parent.as_posix(), "kind": "note" if path.suffix.lower() == ".md" else "file",
                                    "suffix": path.suffix.lower(), "size": 0, "modified_ns": 0, "missing": True,
                                    "tags": [], "favorite": False, "projects": [], "agents": [], "search_text": ""})
                members = [member for member in members if member["relative"] not in value.get("excluded", [])]
                members.sort(key=lambda entry: (entry.get("missing", False), entry.get("filename", "").casefold() not in {"skill.md", "readme.md"}, entry["name"].casefold()))
                requested = value.get("entry", "")
                entrance = next((entry for entry in members if entry["relative"] == requested and not entry.get("missing")), None)
                entrance = entrance or next((entry for entry in members if not entry.get("missing") and entry.get("suffix") == ".md"), None)
                missing_count = sum(bool(member.get("missing")) for member in members)
                representative = entrance or (members[0] if members else None)
                base = dict(representative) if representative else {"tags": [], "favorite": False, "projects": [], "agents": [], "notes": "", "collections": [], "accessed_ns": 0}
                for key in ("tags", "projects", "agents", "collections"):
                    base[key] = list(dict.fromkeys(item for member in members for item in member.get(key, [])))
                base["favorite"] = any(member.get("favorite", False) for member in members)
                base["accessed_ns"] = max((member.get("accessed_ns", 0) for member in members), default=0)
                base.update({"relative": "bundle:" + value["id"], "title": value["title"], "name": value["title"], "filename": value["title"], "notes": "",
                             "kind": "bundle", "folder": value.get("folder", ""), "path": representative["path"] if representative else str(self.root / directory),
                             "suffix": "", "size": sum(member["size"] for member in members),
                             "modified_ns": max((member["modified_ns"] for member in members), default=0),
                             "missing": not any(not member.get("missing") for member in members), "missing_members": missing_count,
                             "members": members, "bundle_record": value, "entry_relative": entrance["relative"] if entrance else "",
                             "package_type": "Skill" if directory and any(member.get("filename", "").casefold() == "skill.md" and Path(member["path"]).parent == (Path(directory) if Path(directory).is_absolute() else self.root / directory) for member in members) else "资料合集",
                             "summary": value.get("summary") or f"{len(members)} 份内容 · 文档、链接与附件集中管理",
                             "search_text": ""})
                # Group metadata remains independent of the representative member.
                metadata = self.read_record(base["relative"])
                if re.fullmatch(r"\d+ 份现有资料 · 按专题阅读与复用", base["summary"]):
                    base["summary"] = f"{len(members)} 份现有资料 · 按专题阅读与复用"
                for key in ("title", "summary", "notes", "tags", "favorite", "projects", "agents", "collections", "related", "accessed_ns", "archived", "deleted", "archive_original_directory", "archive_member_locations"):
                    if key in metadata:
                        base[key] = metadata[key]
                base["name"] = base.get("title") or value["title"]
                base["resource_type"] = base["package_type"]
                bundles.append(base)
                covered.update(member["relative"] for member in members)
            except (OSError, ValueError, UserError) as exc:
                errors.append(value["title"] + "：" + str(exc))
        self.raw_items = by_id
        self.bundle_entries = {entry["relative"]: entry for entry in bundles}
        return [entry for entry in items if entry["relative"] not in covered] + bundles
