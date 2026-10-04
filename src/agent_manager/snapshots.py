"""Read-only SQLite snapshots; ordinary files still require stable writers."""
from __future__ import annotations

import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path

from .domain import UserError
from .runtime import TaskContext


def is_sqlite(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(16) == b"SQLite format 3\0"


def is_database_companion(path: Path) -> bool:
    for suffix in ("-wal", "-shm", "-journal"):
        if path.name.endswith(suffix):
            return is_sqlite(path.with_name(path.name[:-len(suffix)]))
    return False


@contextmanager
def snapshot_file(path: Path, context: TaskContext):
    if not is_sqlite(path):
        yield path, False
        return
    with tempfile.TemporaryDirectory(prefix="agent-manager-sqlite-") as temporary:
        target = Path(temporary) / "snapshot.sqlite"
        deadline = time.monotonic() + 60
        def progress(status, remaining, total):
            context.checkpoint()
            if time.monotonic() > deadline:
                raise UserError("数据库持续忙碌，请暂停写入后重试。")
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as source, closing(sqlite3.connect(target)) as destination:
                source.backup(destination, pages=256, progress=progress, sleep=0.05)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise UserError("数据库快照完整性检查失败。")
            context.log("数据库已生成一致性快照。")
            yield target, True
        except sqlite3.Error as exc:
            raise UserError("无法创建数据库快照，请退出原应用后重试。") from exc
