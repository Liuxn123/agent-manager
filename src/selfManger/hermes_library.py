"""Bounded, read-only Hermes session metadata and preview."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .adapters.local import required_directory
from .archives import is_link, safe_name
from .domain import Resource, UserError
from .runtime import TaskContext
from .security import redact


def _home(resource: Resource) -> Path:
    if resource.kind != "hermes_local":
        raise UserError("会话浏览目前用于本地 Hermes。")
    return required_directory(resource.options, "home")


def _file(root: Path, relative: str) -> Path:
    path = root / safe_name(relative)
    if not path.is_file() or any(is_link(parent) for parent in (path, *path.parents) if parent == root or parent.is_relative_to(root)):
        raise UserError("资料文件不存在或包含链接。")
    return path


def _connection(home: Path, context: TaskContext):
    path = _file(home, "state.db")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: int(context.cancel.is_set()), 1000)
    except sqlite3.Error:
        connection.close()
        raise
    return connection


def _time(value) -> str:
    if isinstance(value, (float, int)):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return "未知"
    return str(value or "未知")[:40]


def list_library(resource: Resource, context: TaskContext) -> dict:
    home = _home(resource)
    sessions = []
    note = "会话标题来自本机，阅读不会更改原应用；不展示系统提示词、登录凭据或工具调用数据。"
    if (home / "state.db").exists():
        try:
            with closing(_connection(home, context)) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
                if not {"id", "started_at"}.issubset(columns):
                    raise UserError("此 Hermes 会话数据库格式尚不支持，请在原应用查看。")
                optional = [name for name in ("title", "source", "model", "message_count", "archived", "last_activity_at") if name in columns]
                fields = ["id", "started_at", *optional]
                order = "coalesce(last_activity_at,started_at)" if "last_activity_at" in columns else "started_at"
                visible = " WHERE coalesce(hidden,0)=0" if "hidden" in columns else ""
                for row in db.execute("SELECT " + ",".join(fields) + " FROM sessions" + visible + " ORDER BY " + order + " DESC LIMIT 100"):
                    context.checkpoint()
                    values = dict(row)
                    sessions.append({"id": str(values["id"]), "title": redact(str(values.get("title") or "未命名会话"))[:200],
                                     "source": redact(str(values.get("source") or "—"))[:40], "model": redact(str(values.get("model") or "—"))[:80],
                                     "messages": values.get("message_count", "—"), "time": _time(values.get("last_activity_at") or values["started_at"]),
                                     "archived": bool(values.get("archived"))})
        except sqlite3.Error as exc:
            context.checkpoint()
            raise UserError("会话数据库暂时无法读取，请稍后重试。") from exc
    else:
        note += " 运行目录里没有 state.db，无法列出本地会话。"
    return {"sessions": sessions, "note": note + " 最多列 100 个近期会话；隐藏会话不列出。"}


def read_item(resource: Resource, category: str, identity: str, context: TaskContext) -> dict:
    home = _home(resource)
    context.checkpoint()
    if category != "session" or not isinstance(identity, str) or not 0 < len(identity) <= 512:
        raise UserError("会话编号不正确。")
    try:
        with closing(_connection(home, context)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            hidden = " AND coalesce(hidden,0)=0" if "hidden" in columns else ""
            if not db.execute("SELECT 1 FROM sessions WHERE id=?" + hidden, (identity,)).fetchone():
                raise UserError("这个会话不存在或已隐藏，请刷新列表。")
            message_columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
            if not {"id", "session_id", "role", "content"}.issubset(message_columns):
                raise UserError("此消息数据库格式暂不支持浏览。")
            filters = (" AND coalesce(active,1)=1" if "active" in message_columns else "")
            rows = db.execute("SELECT role,substr(content,1,2500) AS content FROM messages WHERE session_id=? AND role IN ('user','assistant')" + filters + " ORDER BY id DESC LIMIT 20", (identity,)).fetchall()
            parts = []
            for row in reversed(rows):
                context.checkpoint()
                parts.append(("你" if row["role"] == "user" else "Hermes") + "\n" + redact(str(row["content"] or "")))
            return {"text": "\n\n".join(parts) + "\n\n[仅显示最近 20 条用户/助手消息，每条最多 2500 字；完整记录请在原 Hermes 查看。]"}
    except sqlite3.Error as exc:
        context.checkpoint()
        raise UserError("会话内容暂时无法读取，请稍后重试。") from exc
