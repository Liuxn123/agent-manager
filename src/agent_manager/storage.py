from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .domain import Resource, UserError
from .portable import portable_directory, encode_paths, decode_paths


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def data_directory() -> Path:
    if override := os.environ.get("AGENT_MANAGER_DATA_DIR"):
        return Path(override).expanduser().resolve()
    if portable := portable_directory():
        return portable / "data"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "AgentManager"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/AgentManager"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "agent-manager"


def validate_public_config(value: Any) -> None:
    """Reject credential values; paths and OS credential identifiers are permitted."""
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = key.lower()
            if lowered in {"password", "passphrase", "token", "api_key", "secret", "private_key", "access_token"}:
                raise UserError("配置不能保存明文凭据，请使用凭据引用或受保护文件。")
            validate_public_config(item)
    elif isinstance(value, list):
        for item in value:
            validate_public_config(item)
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise UserError("配置包含不支持的值。")


class Store:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or data_directory()).resolve()
        self.portable_root = self.root.parent if (self.root.parent / "portable.json").is_file() else None
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "manager.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS resources (id TEXT PRIMARY KEY, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evidence (key TEXT PRIMARY KEY, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, resource_id TEXT, title TEXT, state TEXT,
                    started_at TEXT, finished_at TEXT, log TEXT, result TEXT
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def resources(self) -> list[Resource]:
        with self.connect() as db:
            return [Resource.from_dict(decode_paths(json.loads(row[0]), self.portable_root)) for row in db.execute("SELECT document FROM resources ORDER BY rowid")]

    def save_resource(self, resource: Resource) -> None:
        Resource.from_dict(resource.to_dict())
        validate_public_config(resource.options)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO resources VALUES (?, ?)", (resource.id, json.dumps(encode_paths(resource.to_dict(), self.portable_root), ensure_ascii=False)))

    def remove_resource(self, identity: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM resources WHERE id=?", (identity,))

    def setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return decode_paths(json.loads(row[0]), self.portable_root) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        validate_public_config({key: value})
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(encode_paths(value, self.portable_root), ensure_ascii=False)))

    def evidence(self, key: str) -> dict:
        key = str(encode_paths(key, self.portable_root))
        with self.connect() as db:
            row = db.execute("SELECT document FROM evidence WHERE key=?", (key,)).fetchone()
        return decode_paths(json.loads(row[0]), self.portable_root) if row else {}

    def save_evidence(self, key: str, report: dict) -> None:
        from .security import safe_result
        key = str(encode_paths(key, self.portable_root))
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO evidence VALUES (?, ?)", (key, json.dumps(encode_paths(safe_result(report), self.portable_root), ensure_ascii=False)))

    def export_config(self, destination: Path) -> None:
        payload = {"schema_version": 1, "resources": [r.to_dict() for r in self.resources()]}
        validate_public_config(payload)
        destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def import_config(self, source: Path) -> int:
        if source.stat().st_size > 2_000_000:
            raise UserError("配置文件过大。")
        data = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("resources"), list):
            raise UserError("不支持的配置格式。")
        validate_public_config(data)
        resources = [Resource.from_dict(item) for item in data["resources"]]
        if len(resources) > 1000:
            raise UserError("资源数量过多。")
        with self.connect() as db:
            count = 0
            for resource in resources:
                count += db.execute("INSERT OR IGNORE INTO resources VALUES (?, ?)", (resource.id, json.dumps(encode_paths(resource.to_dict(), self.portable_root), ensure_ascii=False))).rowcount
        return count

    def recover_interrupted(self) -> None:
        with self.connect() as db:
            db.execute("UPDATE tasks SET state='interrupted', finished_at=? WHERE state IN ('running','queued')", (now(),))

    def start_task(self, identity: str, resource_id: str, title: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO tasks VALUES (?, ?, ?, 'running', ?, NULL, '', '{}')", (identity, resource_id, title, now()))

    def append_log(self, identity: str, message: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE tasks SET log=substr(log || ?, -40000) WHERE id=?", (f"{now()}  {message}\n", identity))

    def finish_task(self, identity: str, state: str, result: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute("UPDATE tasks SET state=?, finished_at=?, result=? WHERE id=?", (state, now(), json.dumps(result, ensure_ascii=False), identity))

    def tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tasks ORDER BY rowid DESC LIMIT ?", (limit,))]
