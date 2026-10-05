"""Backup evidence and conservative schedules shared by the dashboard and UI."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import archives
from .domain import Resource
from .storage import Store


def age_hours(value: str | None, current: datetime | None = None) -> float | None:
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0, ((current or datetime.now(timezone.utc)) - stamp).total_seconds() / 3600)
    except (TypeError, ValueError):
        return None


def fingerprint(path: Path) -> dict:
    stat = path.stat()
    return {"file_size": stat.st_size, "modified_ns": stat.st_mtime_ns}


def backup_health(store: Store, resource: Resource, backup_root: Path) -> dict:
    if resource.kind in {"project", "vault", "agent"}:
        candidates = archives.list_archives(backup_root, resource.id)
        if not candidates:
            return {"state": "未备份", "source": "", "created_at": None, "verified": False, "rehearsed": False}
        newest = candidates[0]
        path = Path(newest["archive"])
        evidence = store.evidence(str(path.resolve()))
        try:
            matches = all(evidence.get(key) == value for key, value in fingerprint(path).items())
        except OSError:
            return {"state": "备份已移走", "source": "", "created_at": None, "verified": False, "rehearsed": False}
        verified = bool(matches and evidence.get("verified_at"))
        rehearsed = bool(matches and evidence.get("rehearsed_at"))
        age = age_hours(newest.get("created_at"))
        return {**newest, "source": str(path), "verified": verified, "rehearsed": rehearsed,
                "state": "已过期" if age is not None and age > 48 else "已演练" if rehearsed else "已校验" if verified else "待校验"}
    observation = store.evidence("observe:" + resource.id)
    created = observation.get("backup_created_at") or observation.get("created_at")
    if resource.kind == "hermes_local":
        metadata = Path(resource.options.get("backup_repo", "")) / "snapshot/snapshot.json"
        try:
            if metadata.is_file() and metadata.stat().st_size < 20_000_000:
                created = json.loads(metadata.read_text(encoding="utf-8")).get("created_at")
        except (OSError, ValueError):
            return {"state": "快照读取失败", "created_at": None, "source": "", "verified": False, "rehearsed": False}
    evidence = store.evidence("native:" + resource.id)
    verified = bool(created and evidence.get("created_at") == created and evidence.get("verified_at"))
    age = age_hours(created)
    return {"state": "未检测" if not created else "已过期" if age is not None and age > 48 else "已校验" if verified else "待校验",
            "source": "", "created_at": created, "verified": verified, "rehearsed": False}


def is_due(store: Store, resource: Resource, current: datetime | None = None) -> bool:
    if not resource.options.get("automatic_backup") or resource.kind == "hermes_server":
        return False
    policy = store.evidence("schedule:" + resource.id)
    age = age_hours(policy.get("last_attempt_at"), current)
    hours = max(1, min(168, int(resource.options.get("backup_interval_hours", 24))))
    return age is None or age >= hours


def retention_candidates(root: Path, resource_id: str, keep: int, protected: str) -> list[Path]:
    """Only archives created for this resource, inside this root, are eligible."""
    keep = max(1, min(100, keep))
    result = []
    for item in archives.list_archives(root, resource_id)[keep:]:
        source = Path(item["archive"])
        if source.resolve().parent == root.resolve() and source.name != Path(protected).name and not source.is_symlink() and source.stem.isalnum():
            result.append(source)
    return result
