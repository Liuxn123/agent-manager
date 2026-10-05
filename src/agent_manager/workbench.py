"""Markdown sources for everyday work. No backup schema or project registry changes."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import yaml

from .domain import UserError

TYPES = {"prompt": "Prompt", "skill": "Skill", "mcp": "MCP", "agent": "Agent", "tool": "工具",
         "website": "网站", "github": "GitHub 仓库", "article": "文章 / 资料", "template": "模板", "other": "其他"}
PHASES = ("规划", "进行中", "测试", "收尾", "完成")
SUMMARY_HEADINGS = ("阶段目标", "完成内容", "验证结果", "遇到的问题", "失败方案", "重要决策", "经验与反思", "下一阶段计划")
MAX_TEXT = 1_000_000


def migrate_workbench(store):
    """Additive, repeatable configuration migration; never rewrites user documents."""
    version = store.setting("workbench_schema", 0)
    if version not in (0, 1):
        raise UserError("工作台数据来自更新版本，请使用对应版本打开。")
    if not version:
        store.set_setting("workbench_schema", 1)


def work_root(store):
    configured = store.setting("workbench_root", "")
    if configured:
        return Path(configured).expanduser()
    workspace = store.setting("project_workspace", "")
    if workspace and (Path(workspace) / "myself").is_dir():
        return Path(workspace) / "myself/Agent工作台"
    return store.root / "工作台"


def frontmatter(text):
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match:
        return {}, text
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise UserError("Markdown 顶部的 frontmatter 格式不正确。") from exc
    if not isinstance(metadata, dict):
        raise UserError("frontmatter 必须是名称与值的映射。")
    return metadata, text[match.end():]


def render_document(metadata, body):
    return "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).rstrip() + "\n---\n\n" + body.lstrip("\n")


class MarkdownFiles:
    """Bounded regular files, exclusive writes and compare-before-replace for external editors."""
    def __init__(self, root):
        requested = Path(root).expanduser().absolute()
        if requested.is_symlink() or getattr(requested, "is_junction", lambda: False)():
            raise UserError("工作台资料根目录不能是链接。")
        # /var on macOS and some Windows system directories are legitimate parent aliases.
        # Anchor their actual parent, while rejecting links within the configured root.
        self.root = requested.parent.resolve() / requested.name

    def path(self, relative):
        parts = Path(relative).parts
        if not parts or Path(relative).is_absolute() or any(p in {"..", "."} or ":" in p for p in parts):
            raise UserError("资料路径必须位于工作台目录内。")
        target = self.root / relative
        # Also reject junctions, including existing ancestors of a not-yet-created root.
        for path in (target, *target.parents):
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise UserError("工作台资料不能经过符号链接或目录联接。")
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise UserError("资料路径越过工作台目录。")
        return target

    def read(self, relative):
        path = self.path(relative)
        if not path.exists():
            return None
        if not path.is_file() or path.stat().st_size > MAX_TEXT:
            raise UserError("资料不是普通文件或超过 1 MB。")
        try:
            return path.read_text(encoding="utf-8-sig")
        except UnicodeError as exc:
            raise UserError("资料需要使用 UTF-8 编码。") from exc

    @contextmanager
    def lock(self):
        self.path(".write.lock")
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".write.lock"
        try:
            handle = lock.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise UserError("另一项资料写入正在进行，请稍后重试。") from exc
        try:
            with handle:
                handle.write(str(os.getpid()))
            yield
        finally:
            lock.unlink(missing_ok=True)

    def write(self, relative, text, expected):
        if len(text.encode("utf-8")) > MAX_TEXT:
            raise UserError("正文超过 1 MB，请拆分资料。")
        with self.lock():
            path = self.path(relative)
            if self.read(relative) != expected:
                raise UserError("文件已被 Obsidian 或其他程序修改。请刷新后再编辑；原文件未覆盖。")
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as output:
                    output.write(text)
                    output.flush()
                    os.fsync(output.fileno())
                if self.read(relative) != expected:
                    raise UserError("文件在保存期间发生变化，请刷新后重试。")
                if expected is None:
                    # Exclusive creation closes the race with an external editor creating this file.
                    with path.open("x", encoding="utf-8", newline="\n") as output:
                        output.write(text)
                else:
                    os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return {"path": str(path)}


class Daily:
    def __init__(self, root, day=None):
        self.files = MarkdownFiles(root)
        self.day = day or date.today()
        self.relative = "每日/" + self.day.isoformat() + ".md"

    def template(self):
        return f"# {self.day.isoformat()}\n\n## 日程\n\n## 今日任务\n\n## 工作记录\n\n"

    def load(self):
        original = self.files.read(self.relative)
        text = self.template() if original is None else original
        schedule, tasks, section = [], [], ""
        for line, value in enumerate(text.splitlines()):
            if value.startswith("## "):
                section = value[3:].strip()
            match = re.match(r"^\s*[-*] (?:\[([ xX])\] )?(.*)$", value)
            if match and section in {"日程", "今日任务"}:
                entry = {"line": line, "text": match.group(2), "done": match.group(1) in {"x", "X"}}
                (schedule if section == "日程" else tasks).append(entry)
        return {"text": text, "original": original, "schedule": schedule, "tasks": tasks, "path": str(self.files.path(self.relative))}

    def save(self, text, expected):
        return self.files.write(self.relative, text, expected)

    def toggle(self, line, done, expected):
        if expected is None:
            raise UserError("请先添加今日任务。")
        lines = expected.splitlines(keepends=True)
        if not 0 <= line < len(lines) or not re.match(r"^\s*[-*] (?:\[[ xX]\] )?.+", lines[line]):
            raise UserError("任务位置已改变，请刷新。")
        lines[line] = re.sub(r"^(\s*[-*] )(?:\[[ xX]\] )?", lambda m: m[1] + ("[x] " if done else "[ ] "), lines[line])
        return self.save("".join(lines), expected)


class Catalog:
    def __init__(self, root):
        self.files = MarkdownFiles(root)
        self.cache = {}
        self.guard = threading.Lock()

    def parse(self, relative, text):
        if text is None:
            raise UserError("资源文件已移走，请刷新。")
        metadata, body = frontmatter(text)
        identity = metadata.get("id")
        # Existing hand-written resources can be indexed without changing their files.
        if not identity:
            identity = "file:" + relative
        if not isinstance(identity, str) or len(identity) > 240:
            raise UserError("资源 id 不正确。")
        if metadata.get("type", "other") not in TYPES or not isinstance(metadata.get("name", Path(relative).stem), str):
            raise UserError("资源类型或名称不正确。")
        for key in ("tags", "agents", "projects"):
            if key in metadata and (not isinstance(metadata[key], list) or any(not isinstance(v, str) for v in metadata[key])):
                raise UserError(key + " 应为文本列表。")
        for key in ("favorite", "configured", "tested", "installed"):
            if key in metadata and not isinstance(metadata[key], bool):
                raise UserError(key + " 应为 true 或 false。")
        return {"id": identity, "relative": relative, "metadata": metadata, "body": body, "text": text,
                "name": metadata.get("name", Path(relative).stem), "type": metadata.get("type", "other"),
                "path": str(self.files.path(relative))}

    def scan(self, context=None):
        with self.guard:
            root = self.files.path("资源")
            if not root.exists():
                return {"items": [], "errors": []}
            items, errors, seen, current = [], [], set(), set()
            # Deliberately flat: avoid unbounded walks or following linked subdirectories.
            for index, path in enumerate(root.glob("*.md")):
                if context:
                    context.checkpoint()
                if index >= 1000:
                    errors.append("超过 1000 个资源，请分库整理。")
                    break
                relative = "资源/" + path.name
                try:
                    checked = self.files.path(relative)
                    stamp = (checked.stat().st_mtime_ns, checked.stat().st_size)
                    current.add(relative)
                    cached = self.cache.get(relative)
                    if cached and cached[0] == stamp:
                        item = cached[1]
                    else:
                        item = self.parse(relative, self.files.read(relative))
                        self.cache[relative] = (stamp, item)
                    if item["id"] in seen:
                        raise UserError("资源 id 重复，请在 Obsidian 修正。")
                    seen.add(item["id"])
                    items.append(item)
                except (UserError, OSError) as exc:
                    errors.append(path.name + "：" + str(exc))
            self.cache = {key: value for key, value in self.cache.items() if key in current}
            return {"items": sorted(items, key=lambda i: (not i["metadata"].get("favorite", False), i["name"].casefold())), "errors": errors}

    def save(self, metadata, body, item=None):
        metadata = dict(metadata)
        if item:
            metadata = {**item["metadata"], **metadata}
            metadata.setdefault("id", item["id"])
            relative, expected = item["relative"], item["text"]
        else:
            metadata.setdefault("id", uuid.uuid4().hex)
            relative, expected = "资源/" + uuid.uuid4().hex + ".md", None
        if not str(metadata.get("name", "")).strip() or len(metadata["name"]) > 120:
            raise UserError("请填写 1–120 字的资源名称。")
        text = render_document(metadata, body)
        self.parse(relative, text)
        return self.files.write(relative, text, expected)


def project_context(status, state="candidate"):
    metadata, _ = frontmatter(status)
    phase = metadata.get("phase")
    old = re.search(r"(?m)^\s*[-*] 当前阶段[：:]\s*(.+)$", status)
    if not isinstance(phase, str) or not phase.strip():
        old_phase = old[1].strip().rstrip("。") if old else ""
        phase = old_phase if old_phase and len(old_phase) <= 20 and "待明确" not in old_phase else {"candidate": "规划", "active": "进行中", "completed": "完成", "archived": "完成"}.get(state, "进行中")
    next_step = re.search(r"(?m)^\s*[-*] 下一步[：:]\s*(.+)$", status)
    relations = {}
    for key in ("agents", "resources"):
        values = metadata.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise UserError("项目 " + key + " 字段应为编号列表。")
        relations[key] = values
    return {"phase": phase, "next_step": next_step[1].strip() if next_step else "在任务入口补充下一步。", **relations}


def patch_fields(text, values):
    """Patch only managed top-level fields; keep comments, unrelated metadata and body verbatim."""
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match:
        raise UserError("项目 STATUS 缺少 frontmatter，请先修复原文件。")
    header = match[1]
    for key, value in values.items():
        replacement = key + ": " + json.dumps(value, ensure_ascii=False)
        pattern = r"(?m)^" + re.escape(key) + r":[^\n]*(?:\n(?:[ \t]+[^\n]*|-[^\n]*))*"
        header = re.sub(pattern, lambda _: replacement, header) if re.search(pattern, header) else header + "\n" + replacement
    return "---\n" + header + "\n---\n" + text[match.end():]


def set_project_details(workspace, identity, phase, agents, resources, expected, context):
    if not isinstance(phase, str) or not 1 <= len(phase.strip()) <= 60 or "\n" in phase:
        raise UserError("阶段名称需要 1–60 字。")
    for values in (agents, resources):
        if not isinstance(values, list) or len(values) > 100 or any(not isinstance(v, str) or len(v) > 240 for v in values):
            raise UserError("关联资料编号不正确。")
    text = patch_fields(expected, {"phase": phase.strip(), "agents": agents, "resources": resources})
    # Only mirror known stage labels; legacy free-form progress remains untouched.
    old_metadata, _ = frontmatter(expected)
    mirrored_phases = {*PHASES, "待明确"}
    if isinstance(old_metadata.get("phase"), str):
        mirrored_phases.add(old_metadata["phase"])
    text = re.sub(r"(?m)^([-*] 当前阶段[：:])([^\n]*)$",
                  lambda m: m[1] + " " + phase.strip() if m[2].strip().rstrip("。") in mirrored_phases else m[0], text)
    return workspace.save_document(identity, "agent/STATUS.md", text, expected, context)


def stage_template(phase):
    return "阶段：" + phase + "\n\n" + "\n\n".join("### " + heading + "\n\n" for heading in SUMMARY_HEADINGS)


def stage_prompt(name, phase, status, tasks):
    return (f"请为项目 {name} 的“{phase}”阶段整理总结。只依据下面的记录，不臆测完成或验证；不确定项明确写待确认。"
            "保留失败方案、决策原因和可核验结果，按模板填写。不要执行命令或修改文件。\n\n"
            + stage_template(phase) + "\n\n## 状态原文\n" + status + "\n\n## 任务原文\n" + tasks)


def append_stage(workspace, identity, phase, body, context):
    if not body.strip() or not all("### " + heading in body for heading in SUMMARY_HEADINGS):
        raise UserError("阶段总结需保留八个栏目；没有内容的栏目可填写“暂无”。")
    content = re.sub(r"(?m)^### .+$|^阶段：.+$", "", body).strip()
    if not content:
        raise UserError("请至少填写一项阶段记录，再保存总结。")
    # Existing append_log provides the lock, archive guard and atomic append.
    return workspace.append_log(identity, "**阶段总结：" + phase.replace("\n", " ") + "**\n\n" + body, context)


def summaries(handoff):
    headings = list(re.finditer(r"(?m)^## .+$", handoff))
    return [handoff[m.start():headings[i + 1].start() if i + 1 < len(headings) else len(handoff)]
            for i, m in enumerate(headings) if "**阶段总结：" in handoff[m.end():headings[i + 1].start() if i + 1 < len(headings) else len(handoff)]]


def markdown_uri(path):
    path = Path(path).absolute()
    if not path.is_file() or path.suffix.lower() != ".md":
        raise UserError("请先保存 Markdown 文件。")
    if not any((parent / ".obsidian").is_dir() for parent in path.parents):
        raise UserError("请先在 Obsidian 打开这份资料所在的文件夹为知识库；管家不会修改知识库设置。")
    return "obsidian://open?" + urlencode({"path": str(path), "paneType": "tab"})
