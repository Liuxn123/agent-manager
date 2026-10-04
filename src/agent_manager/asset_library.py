"""Local skill inventory and portable copies; never executes client configuration."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlsplit
import yaml

from .archives import canonical_system_path, is_link
from .domain import Resource, UserError
from .runtime import TaskContext
from .security import redact

LIMIT = 1024 * 1024


def library_root(store) -> Path:
    return store.root / "asset-library"


def checked(path: Path) -> Path:
    path = canonical_system_path(path.expanduser())
    if any(is_link(parent) for parent in (path, *path.parents)):
        raise UserError("资料路径包含链接，请选择真实目录。")
    return path


def text_file(path: Path) -> str:
    path = checked(path)
    if not path.is_file() or path.stat().st_size > LIMIT:
        raise UserError("文件不存在或超过 1 MiB，无法在这里查看。")
    with path.open("rb") as reader:
        data = reader.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise UserError("文件过大。")
    return data.decode("utf-8-sig", "replace")


def name_for(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[\w\-\u4e00-\u9fff][\w\-\u4e00-\u9fff .]{0,79}", value) or value.endswith((".", " ")) or value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *[f"{name}{i}" for name in ("COM", "LPT") for i in range(1, 10)]}:
        raise UserError("名称请用汉字、字母、数字或短横线，最多 80 字。")
    return value


def roots_for(store) -> list[dict]:
    roots = [{"owner": "管家通用库", "path": str(library_root(store)), "managed": True}]
    for resource in store.resources():
        if resource.kind not in {"hermes_local", "agent"}:
            continue
        paths = [resource.options.get("home")] if resource.kind == "hermes_local" else [resource.options.get("path"), *resource.options.get("record_paths", [])]
        for value in paths:
            if value:
                roots.append({"owner": resource.name, "path": value, "managed": False})
        if resource.options.get("engine") == "Codex":
            roots.append({"owner": "Codex · 共用技能", "path": str(Path.home() / ".agents"), "managed": False})
    for root in store.setting("asset_roots", []):
        if isinstance(root, dict) and isinstance(root.get("path"), str):
            roots.append({"owner": str(root.get("owner", "自选目录"))[:120], "path": root["path"], "managed": False, "skills_only": bool(root.get("skills_only"))})
    result, seen = [], set()
    for root in roots:
        key = os.path.normcase(os.path.abspath(root["path"]))
        if key not in seen:
            seen.add(key)
            result.append(root)
    return result


def walk_files(root: Path, context: TaskContext, *, depth: int = 4, maximum: int = 200):
    visited = 0
    if not root.is_dir() or is_link(root):
        return
    for current, directories, files in os.walk(root, followlinks=False):
        context.checkpoint()
        visited += 1
        if visited > maximum:
            return
        path = Path(current)
        level = len(path.relative_to(root).parts)
        directories[:] = [name for name in sorted(directories) if level < depth and not name.startswith(".") and name not in {"node_modules", "cache", "logs"} and not is_link(path / name)]
        for name in sorted(files):
            file = path / name
            if not is_link(file):
                yield file


def mcp_entries(path: Path) -> list[dict]:
    text = text_file(path)
    try:
        if path.suffix == ".toml":
            document = tomllib.loads(text)
        elif path.suffix in {".yaml", ".yml"}:
            document = yaml.safe_load(text)
        else:
            document = json.loads(text)
    except (ValueError, RecursionError, yaml.YAMLError) as exc:
        raise UserError("连接文件格式不正确。") from exc
    if not isinstance(document, dict):
        return []
    servers = document.get("mcp_servers", document.get("mcpServers", {}))
    if not isinstance(servers, dict):
        return []
    result = []
    for name, config in list(servers.items())[:100]:
        if not isinstance(config, dict):
            continue
        endpoint = str(config.get("url", config.get("serverUrl", "")))
        try:
            parsed = urlsplit(endpoint)
            endpoint = parsed.scheme + "://" + (parsed.hostname or "") if endpoint else ""
        except ValueError:
            endpoint = "地址格式需检查"
        # No env values, args, headers, URL queries/userinfo, or credential fields.
        public = {"名称": redact(str(name))[:120], "方式": "远程连接" if endpoint else "本地程序",
                  "地址": endpoint, "程序": redact(Path(str(config.get("command", ""))).name)[:120],
                  "环境变量名": [redact(str(key))[:80] for key in list(config.get("env", {}))[:30]] if isinstance(config.get("env"), dict) else [],
                  "启用": not bool(config.get("disabled")) and config.get("enabled", True) is not False}
        result.append(public)
    return result


def scan(store, context: TaskContext) -> dict:
    items, notes, roots = [], [], roots_for(store)
    for root in roots[:40]:
        context.checkpoint()
        try:
            path = checked(Path(root["path"]))
            if not path.is_dir():
                continue
            skills = path if root.get("skills_only") else path / "skills"
            for file in walk_files(skills, context):
                if file.name == "SKILL.md":
                    items.append({"category": "skill", "name": file.parent.name, "owner": root["owner"], "path": str(file), "managed": root["managed"]})
            if root.get("skills_only"):
                continue
            for name in ("config.toml", "config.yaml", "mcp.json", ".mcp.json", "mcp-config.json"):
                file = path / name
                if not file.is_file():
                    continue
                for entry in mcp_entries(file):
                    items.append({"category": "mcp", "name": entry["名称"], "owner": root["owner"], "path": str(file), "public": entry, "managed": False})
            for file in walk_files(path / "mcp", context, depth=1, maximum=100):
                if file.suffix == ".json":
                    for entry in mcp_entries(file):
                        items.append({"category": "mcp", "name": entry["名称"], "owner": root["owner"], "path": str(file), "public": entry, "managed": root["managed"]})
            prompts = [path / name for name in ("AGENTS.md", "CLAUDE.md", "SOUL.md", "USER.md", "instructions.md")]
            prompts += list(walk_files(path / "prompts", context, depth=2, maximum=100))
            for file in prompts:
                if file.is_file() and file.suffix.lower() in {".md", ".txt"} and not is_link(file):
                    items.append({"category": "prompt", "name": file.stem, "owner": root["owner"], "path": str(file), "managed": root["managed"]})
        except (UserError, OSError, ValueError) as exc:
            notes.append(root["owner"] + "：目录或配置暂不可读，请核对来源。")
        if len(items) >= 1500:
            break
    for filename in store.setting("asset_mcp_files", [])[:20]:
        context.checkpoint()
        try:
            file = checked(Path(filename))
            for entry in mcp_entries(file):
                items.append({"category": "mcp", "name": entry["名称"], "owner": "自选 MCP · " + file.stem,
                              "path": str(file), "public": entry, "managed": False})
        except (UserError, OSError, ValueError):
            notes.append("自选 MCP 文件暂不可读，请重新选择。")
    return {"items": items[:1500], "roots": roots, "notes": notes}


def preview(store, item: dict, context: TaskContext) -> dict:
    context.checkpoint()
    if item["category"] == "mcp":
        public = item["public"]
        lines = ["名称：" + public["名称"], "连接方式：" + public["方式"], "配置中已启用：" + ("是" if public["启用"] else "否")]
        if public["地址"]:
            lines.append("服务主机：" + public["地址"])
        if public["程序"]:
            lines.append("启动程序：" + public["程序"])
        lines.append("需要的环境变量：" + ("、".join(public["环境变量名"]) or "未填写"))
        return {"text": "\n".join(lines) + "\n\n这是配置清单，未检查工具是否已连接。这里只列连接说明，不显示启动参数、环境变量值或登录材料。请在原 Agent 中修改和启用连接。"}
    path = checked(Path(item["path"]))
    roots = []
    for root in roots_for(store):
        try:
            if Path(root["path"]).exists():
                roots.append(checked(Path(root["path"])))
        except UserError:
            continue
    if not any(path.is_relative_to(root) for root in roots) or path.name.lower() in {"auth.json", ".env"}:
        raise UserError("此文件不在已登记的资料目录内。")
    text = text_file(path)
    return {"text": redact(text) + ("\n\n[文件较长，最多显示 40000 字。完整文件在原目录中。]" if len(text) > 40000 else "")}


def copy_skill(source: Path, skills_root: Path, context: TaskContext) -> dict:
    source, destination = checked(source), checked(skills_root)
    if not source.is_dir() or not (source / "SKILL.md").is_file():
        raise UserError("请选择直接包含 SKILL.md 的技能文件夹。")
    name = name_for(source.name)
    target = destination / name
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(target):
        raise UserError("来源与目标目录重叠，请重新选择。")
    if target.exists():
        raise UserError("目标已有同名技能，保留原文件。请改名或在原应用处理冲突。")
    files, directories_to_copy, total = [], [], 0
    for current, directories, filenames in os.walk(source, followlinks=False):
        context.checkpoint()
        directory = Path(current)
        directories_to_copy.append(directory.relative_to(source))
        if len(directories_to_copy) > 1000:
            raise UserError("技能目录数量过多，最多 1000 个目录。")
        if len(directory.relative_to(source).parts) > 8:
            raise UserError("技能目录层级过深。")
        if any(is_link(directory / name) for name in [*directories, *filenames]):
            raise UserError("技能含链接，未复制。")
        directories[:] = [name for name in directories if name not in {".git", "__pycache__", "node_modules"}]
        for filename in filenames:
            file = directory / filename
            if not file.is_file():
                raise UserError("技能包含特殊文件，未复制。")
            size = file.stat().st_size
            total += size
            if len(files) >= 1000 or total > 20 * 1024 * 1024:
                raise UserError("单个技能最多 1000 个文件、20 MiB。")
            digest = file_digest(file, context)
            files.append((file, file.relative_to(source), digest))
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".agent-skill-", dir=destination) as temporary:
        staged = Path(temporary) / name
        staged.mkdir()
        for relative in directories_to_copy:
            (staged / relative).mkdir(parents=True, exist_ok=True)
        for file, relative, digest in files:
            context.checkpoint()
            copy = staged / relative
            copy.parent.mkdir(parents=True, exist_ok=True)
            checked(file)
            count = 0
            with file.open("rb") as reader, copy.open("xb") as writer:
                while chunk := reader.read(65536):
                    context.checkpoint()
                    count += len(chunk)
                    if count > 20 * 1024 * 1024:
                        raise UserError("技能在复制期间变化，未安装。")
                    writer.write(chunk)
            shutil.copystat(file, copy)
            if file_digest(copy, context) != digest or file_digest(file, context) != digest:
                raise UserError("来源技能在复制期间变化，未安装。")
        context.checkpoint()
        if target.exists():
            raise UserError("目标出现同名技能，未覆盖。")
        staged.rename(target)
    return {"copied": len(files), "target": str(target), "note": "已复制技能文件；没有执行其中的脚本。原 Agent 是否支持该技能需在原应用确认。"}


def file_digest(path: Path, context: TaskContext) -> str:
    digest, size = hashlib.sha256(), 0
    with checked(path).open("rb") as reader:
        while chunk := reader.read(65536):
            context.checkpoint()
            size += len(chunk)
            if size > 20 * 1024 * 1024:
                raise UserError("技能文件过大。")
            digest.update(chunk)
    return digest.hexdigest()


def save_prompt(store, name: str, text: str, expected: str | None = None) -> dict:
    if not text.strip() or len(text.encode("utf-8")) > LIMIT:
        raise UserError("提示词不能为空，最多 1 MiB。")
    root = checked(library_root(store) / "prompts")
    target = root / (name_for(name) + ".md")
    if target.exists():
        if expected is None or text_file(target) != expected:
            raise UserError("已有同名提示词或文件已在外部更改，请刷新后再编辑。")
    elif expected is not None:
        raise UserError("原提示词已移走，请刷新列表。")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp", dir=root, delete=False) as writer:
        writer.write(text)
        temporary = Path(writer.name)
    try:
        if expected is None:
            # Exclusive creation preserves an existing file even on POSIX.
            with target.open("x", encoding="utf-8") as output:
                output.write(text)
        else:
            if text_file(target) != expected:
                raise UserError("文件已变更，请刷新。")
            os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"target": str(target)}
