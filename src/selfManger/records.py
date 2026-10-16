"""Bounded, read-only local record browsing; no application database mutations."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .archives import CHUNK, SKIP_DIRS, canonical_system_path, is_link
from .domain import Resource, UserError
from .profiles import sources_for
from .runtime import TaskContext
from .security import redact, safe_result

SECRET_NAMES = {"auth.json", "credentials.json", "secrets.json", "tokens.json", "key.json"}


def list_records(resource: Resource, context: TaskContext) -> dict:
    result = []
    for component in sources_for(resource):
        if component["role"] != "records" and resource.options.get("portable_bundle"):
            continue
        root = Path(component["path"]).expanduser()
        if not root.is_dir() or is_link(root):
            continue
        for current, dirs, files in os.walk(root, followlinks=False):
            context.checkpoint()
            directory = Path(current)
            dirs[:] = [name for name in dirs if name not in SKIP_DIRS | {"cache", "Cache", "tmp", "logs", "worktrees"} and not is_link(directory / name)]
            for name in sorted(files):
                path = directory / name
                if path.suffix.lower() not in {".jsonl", ".md", ".txt", ".json"} or name.lower() in SECRET_NAMES or name.startswith(".env") or is_link(path):
                    continue
                result.append({"name": path.relative_to(root).as_posix(), "path": str(path), "component": component["label"]})
                if len(result) >= 300:
                    return {"records": result, "limited": True}
    return {"records": result, "limited": False}


def _content(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_content(item) for item in value)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("message") or "")
    return ""


def read_record(resource: Resource, filename: str, context: TaskContext) -> dict:
    path = canonical_system_path(Path(filename))
    roots = [Path(item["path"]).expanduser().resolve() for item in sources_for(resource) if item["path"]]
    if not path.is_file() or is_link(path) or any(is_link(parent) for parent in path.parents) or not any(path.resolve().is_relative_to(root) for root in roots):
        raise UserError("记录文件不属于已登记的目录，或文件是链接。")
    if path.name.lower() in SECRET_NAMES or path.name.startswith(".env"):
        raise UserError("登录凭据不作为聊天记录展示。")
    context.checkpoint()
    with path.open("rb") as reader:
        data = reader.read(2 * CHUNK)
        truncated = bool(reader.read(1))
    raw = data.decode("utf-8", "replace")
    if path.suffix.lower() == ".jsonl":
        messages = []
        for line in raw.splitlines():
            context.checkpoint()
            try:
                entry = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload", entry.get("message", entry))
            if not isinstance(payload, dict):
                continue
            role = payload.get("role")
            kind = payload.get("type")
            if role in {"user", "assistant"}:
                content = _content(payload.get("content", payload.get("text", "")))
                label = "你" if role == "user" else "Agent"
            elif kind in {"user_message", "agent_message"}:
                content = _content(payload.get("message", ""))
                label = "你" if kind == "user_message" else "Agent"
            elif "display" in payload and isinstance(payload["display"], str):
                content, label = payload["display"], "历史输入"
            else:
                continue
            if content:
                text = f"{label}\n{content}"
                if not messages or messages[-1] != text:
                    messages.append(text)
        raw = "\n\n".join(messages) if messages else "此文件没有可识别的对话消息。原始资料已保存，可用原应用查看。"
    elif path.suffix.lower() == ".json":
        try:
            raw = json.dumps(safe_result(json.loads(raw)), ensure_ascii=False, indent=2)
        except ValueError:
            raw = "此 JSON 文件较大或格式不完整，请用原应用查看。"
    if truncated:
        raw += "\n\n（仅显示前 2 MiB，备份仍保存完整文件。）"
    return {"text": redact(raw), "truncated": truncated}
