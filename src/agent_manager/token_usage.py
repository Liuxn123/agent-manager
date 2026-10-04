"""Bounded, on-demand usage metadata reader. No daemon, network or chat storage."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .domain import Resource
from .profiles import sources_for
from .runtime import TaskContext

FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
MAX_FILES = 200
TAIL_BYTES = 2 * 1024 * 1024
MAX_BYTES = 64 * 1024 * 1024


def usage_tuple(value):
    if not isinstance(value, dict) or not any(k in value for k in FIELDS):
        return None
    values = []
    for key in FIELDS:
        raw = value.get(key, 0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= raw <= 10**16:
            return None
        values.append(int(raw))
    if "total_tokens" not in value:
        values[-1] = values[0] + values[2]
    return tuple(values)


def parse_usage(data: bytes) -> tuple[int, ...] | None:
    # Match cumulative stream heads so repeated/interleaved snapshots are not charged twice.
    heads, totals = [], [0] * len(FIELDS)
    found = False
    for line in data.splitlines():
        if b'"token_count"' not in line or len(line) > TAIL_BYTES:
            continue
        try:
            event = json.loads(line)
            payload = event.get("payload", {})
            if event.get("type") != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            total = usage_tuple(info.get("total_token_usage"))
            last = usage_tuple(info.get("last_token_usage"))
            if total is None:
                continue
            found = True
            if total in heads:
                continue
            previous = tuple(a - b for a, b in zip(total, last)) if last else None
            if previous in heads:
                heads.remove(previous)
                delta = last
            elif not heads:
                delta = total
            elif last:
                delta = last
            else:
                # No stream identity: report a conservative positive delta.
                delta = tuple(max(0, a - b) for a, b in zip(total, heads[-1]))
            heads.append(total)
            heads = heads[-32:]
            totals = [a + b for a, b in zip(totals, delta)]
        except (ValueError, AttributeError, TypeError):
            continue
    return tuple(totals) if found else None


def read_usage(resource: Resource, context: TaskContext) -> dict:
    if str(resource.options.get("engine", "")).casefold() != "codex":
        return {"状态": "此 Agent 暂无已验证的本地用量解析器", "说明": "仍可管理资料、浏览记录和备份。没有用量字段不代表没有消耗。"}
    paths, seen, limited, errors = [], set(), False, 0
    for component in sources_for(resource):
        if component["role"] != "records":
            continue
        root = Path(component["path"])
        for folder in (root / "sessions", root / "archived_sessions"):
            if folder.is_symlink() or not folder.is_dir():
                continue
            for base, dirs, files in os.walk(folder, followlinks=False):
                context.checkpoint()
                dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink() and not (Path(base) / d).is_junction()] if hasattr(Path, "is_junction") else [d for d in dirs if not (Path(base) / d).is_symlink()]
                for name in files:
                    path = Path(base) / name
                    if not name.endswith(".jsonl") or path.is_symlink():
                        continue
                    identity = str(path.resolve())
                    if identity not in seen:
                        seen.add(identity)
                        paths.append(path)
                    if len(paths) >= MAX_FILES:
                        limited = True
                        break
                if limited:
                    break
            if limited:
                break
        if limited:
            break
    total, read_bytes, available = [0] * len(FIELDS), 0, 0
    # Duplicated exports with the same session filename count once, not per registered root.
    sessions = set()
    for path in paths:
        context.checkpoint()
        if path.name in sessions:
            continue
        sessions.add(path.name)
        try:
            size = path.stat().st_size
            budget = min(size, TAIL_BYTES, MAX_BYTES - read_bytes)
            if budget <= 0:
                limited = True
                break
            with path.open("rb") as handle:
                offset = max(0, size - budget)
                handle.seek(offset)
                data = handle.read(budget)
            read_bytes += len(data)
            if offset:
                data = data.partition(b"\n")[2]
                limited = True
            if data and not data.endswith(b"\n"):
                data = data.rpartition(b"\n")[0]
            usage = parse_usage(data)
            if usage is not None:
                available += 1
                total = [a + b for a, b in zip(total, usage)]
        except OSError:
            errors += 1
    return {"范围": "本地可读取会话的有限统计，非账单；含累积计数，不能据此拆分每日消费", "状态": "读到用量" if available else "没有读到用量字段",
            "会话数": available, "输入 Token": total[0], "其中缓存输入": total[1], "输出 Token": total[2],
            "其中推理输出": total[3], "总 Token": total[4], "读取 MiB": round(read_bytes / 1024**2, 2), "存在截断 / 数量上限": limited,
            "无法读取的文件数": errors, "说明": "缓存和推理是子集，不重复加进总量。最多 200 个文件、每文件末尾 2 MiB、总计 64 MiB。只在点击时读取。"}
