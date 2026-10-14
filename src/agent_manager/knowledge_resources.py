"""Portable resource descriptions and semantic presentation for the knowledge index."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

import yaml

from .domain import UserError


def topic_name(value: str) -> str:
    return re.sub(r"^\d+[-_]", "", value)


def resource_type(entry: dict) -> str:
    if entry["kind"] == "bundle":
        return entry.get("package_type", "资料合集")
    if entry.get("missing"):
        return "位置失效"
    if entry["kind"] == "link":
        return "GitHub 项目" if "github.com" in entry.get("source_url", "").lower() else "网页收藏"
    if entry["kind"] == "external" or entry.get("relative", "").startswith("external:"):
        return "外部引用"
    if entry.get("suffix") in {".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}:
        return "图片素材"
    if entry.get("suffix") in {".py", ".js", ".ts", ".java", ".ps1", ".sh", ".cpp", ".yaml", ".yml"}:
        return "代码片段"
    if "Prompts" in entry.get("folder", ""):
        return "Prompt"
    return "知识笔记" if entry["kind"] == "note" else "参考资料"


def content_summary(text: str) -> str:
    """A literal excerpt, never an invented summary."""
    _, text = note_header(text)
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "```", "!", "|", "<", "---")):
            continue
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = line.replace("[[", "").replace("]]", "").strip("*>- `")
        if len(line) >= 6:
            return line[:150]
    return ""


def note_header(text: str) -> tuple[dict, str]:
    """Read imported headers without swallowing a body when the closing --- is absent."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text
    lines = text.splitlines(keepends=True)
    end = 1
    for end, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            try:
                value = yaml.safe_load("".join(lines[1:end]))
                return value if isinstance(value, dict) else {}, "".join(lines[end + 1:])
            except yaml.YAMLError:
                return {}, text
        if line.lstrip().startswith(("#", "**", "```")):
            break
    else:
        return {}, text
    try:
        value = yaml.safe_load("".join(lines[1:end]))
        return value if isinstance(value, dict) else {}, "".join(lines[end:])
    except yaml.YAMLError:
        return {}, text


class PortableResources:
    """Mixin: sidecar files are metadata truth; SQLite remains a cache."""

    def record_path(self, relative):
        directory = self.root / "99-系统" / "知识库索引" / "资源"
        self._relative(directory)
        return directory / (hashlib.sha256(relative.encode("utf-8")).hexdigest() + ".json")

    def read_record(self, relative):
        path = self.record_path(relative)
        if not path.exists():
            return {}
        self._relative(path)
        if path.stat().st_size > 256_000:
            raise UserError("资源描述记录超过 256 KB。")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("relative") != relative:
            raise UserError("资源描述记录与资料路径不一致。")
        for key in ("tags", "projects", "agents", "collections", "related"):
            if key in value and (not isinstance(value[key], list) or len(value[key]) > 100 or any(not isinstance(item, str) or len(item) > 500 for item in value[key])):
                raise UserError("资源标签或关联记录格式无效。")
        for key in ("title", "summary", "notes"):
            if key in value and not isinstance(value[key], str):
                raise UserError("资源描述格式无效。")
        if "accessed_ns" in value and (not isinstance(value["accessed_ns"], int) or value["accessed_ns"] < 0):
            raise UserError("资源使用时间无效。")
        for key in ("archived", "deleted"):
            if key in value and not isinstance(value[key], bool):
                raise UserError("资源归档状态无效。")
        for key in ("archive_original", "archive_original_directory"):
            if key in value and (not isinstance(value[key], str) or len(value[key]) > 2000):
                raise UserError("资源归档位置无效。")
        if "archive_member_locations" in value:
            locations = value["archive_member_locations"]
            if not isinstance(locations, dict) or len(locations) > 20_000 or any(not isinstance(k, str) or len(k) > 2000 or not isinstance(v, str) or len(v) > 2000 for k, v in locations.items()):
                raise UserError("资源合集归档位置无效。")
        return value

    def write_record(self, relative, values):
        path = self.record_path(relative)
        self._relative(path)
        original = path.read_bytes() if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("." + path.name + "." + uuid4().hex + ".tmp")
        document = {"version": 1, "relative": relative, **values}
        encoded = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if len(encoded) > 256_000:
            raise UserError("资源描述内容过长。")
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
            if (path.read_bytes() if path.exists() else None) != original:
                raise UserError("资源信息已被其他程序修改，请刷新后重试。")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def enrich_resources(self, items, errors):
        for entry in items:
            try:
                text = entry.get("search_text", "")
                frontmatter, _ = note_header(text)
                if frontmatter:
                    if isinstance(frontmatter, dict):
                        if isinstance(frontmatter.get("title"), str):
                            entry["title"] = frontmatter["title"]
                        original_tags = frontmatter.get("tags", [])
                        if isinstance(original_tags, list) and not entry["tags"]:
                            entry["tags"] = [tag for tag in original_tags[:100] if isinstance(tag, str)]
                record = self.read_record(entry["relative"])
                for key in ("tags", "favorite", "projects", "agents", "title", "summary", "notes", "collections", "related", "accessed_ns", "archived", "deleted", "archive_original", "archive_original_directory", "archive_member_locations"):
                    if key in record:
                        entry[key] = record[key]
                entry["name"] = entry.get("title") or entry["name"]
                entry["summary"] = entry.get("summary") or content_summary(entry.get("search_text", ""))
                entry["notes"] = entry.get("notes", "")
                entry["collections"] = entry.get("collections", [])
                entry["related"] = entry.get("related", [])
                entry["accessed_ns"] = entry.get("accessed_ns", 0)
                if entry["kind"] == "link" and not entry.get("missing"):
                    with Path(entry["path"]).open(encoding="utf-8-sig", errors="replace") as source:
                        text = source.read(12_000)
                    entry["source_url"] = next((line[4:].strip() for line in text.splitlines() if line.startswith("URL=")), "")
                entry["resource_type"] = resource_type(entry)
            except (OSError, ValueError, UserError, yaml.YAMLError) as exc:
                errors.append(entry["name"] + "：" + str(exc))
        return items

    def update_resource(self, relative, **changes):
        allowed = {"title", "summary", "notes", "collections", "related", "accessed_ns", "archived", "deleted",
                   "archive_original", "archive_original_directory", "archive_member_locations"}
        if changes.keys() - allowed:
            raise UserError("资源描述包含未知字段。")
        if not relative.startswith(("external:", "bundle:")):
            self._relative(self.root / relative)
        for key in ("title", "summary", "notes"):
            if key in changes:
                if not isinstance(changes[key], str) or len(changes[key]) > (50_000 if key == "notes" else 500):
                    raise UserError("资源标题、用途或笔记超过允许长度。")
        if "collections" in changes:
            changes["collections"] = list(dict.fromkeys(str(v).strip() for v in changes["collections"] if str(v).strip()))
            if len(changes["collections"]) > 50 or any(len(v) > 100 for v in changes["collections"]):
                raise UserError("收藏集最多 50 个，名称最多 100 字。")
        if "related" in changes and (not isinstance(changes["related"], list) or len(changes["related"]) > 100 or any(not isinstance(v, str) or len(v) > 500 for v in changes["related"])):
            raise UserError("关联资料记录无效。")
        record = self.read_record(relative)
        group = getattr(self, "bundle_entries", {}).get(relative)
        if group and not self.bundle_path(group["bundle_record"]["id"]).exists():
            self.change_bundle(group)
        record.update(changes)
        self.write_record(relative, record)
        return record

    def record_access(self, relative):
        timestamp = time.time_ns()
        self.update_resource(relative, accessed_ns=timestamp)
        return timestamp

    def reconcile_paths(self, db):
        """Retain existing SQLite tags after the approved physical reorganization."""
        manifest = self.root / "99-系统" / "知识库索引" / "目录迁移.json"
        if not manifest.is_file():
            return
        self._relative(manifest)
        if manifest.stat().st_size > 2_000_000:
            raise UserError("目录迁移清单过大。")
        mappings = json.loads(manifest.read_text(encoding="utf-8"))["files"]
        for old, new in mappings.items():
            self._relative(self.root / old)
            self._relative(self.root / new)
            new_path = self.root / new
            if (self.root / old).exists() or (not new_path.is_file() and not self.record_path(new).is_file()):
                continue
            row = db.execute("SELECT tags,favorite,projects,agents FROM knowledge_metadata WHERE root_id=? AND relative_path=?", (self.root_id, old)).fetchone()
            if row:
                existing = db.execute("SELECT 1 FROM knowledge_metadata WHERE root_id=? AND relative_path=?", (self.root_id, new)).fetchone()
                if not existing:
                    db.execute("UPDATE knowledge_metadata SET relative_path=? WHERE root_id=? AND relative_path=?", (new, self.root_id, old))
            db.execute("DELETE FROM knowledge_files WHERE root_id=? AND relative_path=?", (self.root_id, old))
            old_record = self.read_record(old)
            if old_record and not self.record_path(new).exists():
                self.write_record(new, {**old_record, "relative": new})
