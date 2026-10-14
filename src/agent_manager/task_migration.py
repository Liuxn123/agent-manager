"""One-way, idempotent import of legacy Markdown tasks into the shared SQLite store."""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

from .domain import UserError
from .project_workspaces import ProjectWorkspace
from .storage import Store
from .workbench import Daily, task_details


def legacy_ref(scope: str, kind: str, text: str, occurrence: int = 0) -> str:
    digest = hashlib.sha256(" ".join(text.split()).casefold().encode("utf-8")).hexdigest()[:20]
    return f"legacy:{scope}:{kind}:{digest}:{occurrence}"


def _daily_names(root: Path) -> list[str]:
    directory = root / "每日"
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise UserError("每日目录无效，未导入旧任务。")
    names = sorted(path.name for path in directory.iterdir()
                   if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", path.name) and not path.is_symlink())
    if len(names) > 5000:
        raise UserError("每日目录超过 5000 个文件，未开始迁移；请先分批整理。")
    return names


def _daily_entries(root: Path, names: set[str] | None = None):
    directory = root / "每日"
    if not directory.exists():
        return
    if directory.is_symlink() or not directory.is_dir():
        raise UserError("每日目录无效，未导入旧任务。")
    candidates = [directory / name for name in _daily_names(root) if names is None or name in names]
    total_bytes = 0
    for path in candidates:
        try:
            day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        total_bytes += path.stat().st_size
        if total_bytes > 50_000_000:
            raise UserError("每日任务资料超过 50 MB，未完成导入。")
        report = Daily(root, day).load()
        for kind, rows in (("task", report["tasks"]), ("schedule", report["schedule"])):
            occurrences = defaultdict(int)
            for row in rows:
                identity = legacy_ref("daily:" + str(root.resolve()) + ":" + day.isoformat(), kind, row["text"], occurrences[row["text"]])
                occurrences[row["text"]] += 1
                title, priority, status = row["text"], "", "done" if row["done"] else "planned"
                if kind == "task":
                    details = task_details(title)
                    title, priority = details["title"], {"高": "high", "中": "medium", "低": "low"}.get(details["priority"], "")
                else:
                    title = re.sub(r"^(?:[01]\d|2[0-3]):[0-5]\d(?:\s*[–-]\s*(?:[01]\d|2[0-3]):[0-5]\d)?\s+", "", title)
                yield {"title": title, "kind": kind, "due_date": day.isoformat(),
                       "priority": priority, "status": status, "source": "manual"}, identity


def _project_entries(workspace: ProjectWorkspace, project_ids: set[str] | None = None):
    for project in workspace.registry()["projects"]:
        if project_ids is not None and project["id"] not in project_ids:
            continue
        path = workspace.path(project["path"] + "/agent/TASKS.md")
        if not path.exists():
            continue
        text = workspace.read(path)
        in_fence = False
        source, table_header, occurrences = "manual", None, defaultdict(int)
        for line in text.splitlines():
            fence = re.match(r"^\s*(`{3,}|~{3,})", line)
            if fence:
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            heading = re.match(r"^#{1,2}\s+(.+?)\s*#*\s*$", line)
            if heading:
                value = re.sub(r"[\s_-]+", "", heading[1]).casefold()
                source = "ai" if ("ai" in value or "agent" in value) and any(word in value for word in ("规划", "计划", "生成")) else "manual"
                table_header = None
                continue
            check = re.match(r"^\s*[-*+] \[([ xX])\]\s+(.+)$", line)
            if check:
                title = check[2].strip()
                occurrence_key = (source, title)
                identity = legacy_ref(f"project:{workspace.root}:{project['id']}:{source}", "task", title, occurrences[occurrence_key])
                occurrences[occurrence_key] += 1
                yield {"title": title, "kind": "task", "project_id": project["id"], "project_root": str(workspace.root),
                       "status": "done" if check[1].casefold() == "x" else "planned", "source": source}, identity
                continue
            if not line.strip().startswith("|"):
                table_header = None
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if "状态" in cells and any(key in cells for key in ("动作", "任务")):
                table_header = (cells.index("状态"), cells.index("动作" if "动作" in cells else "任务"))
            elif table_header and max(table_header) < len(cells) and cells[table_header[1]] and not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                state, title = cells[table_header[0]].casefold(), cells[table_header[1]]
                occurrence_key = (source, title)
                identity = legacy_ref(f"project:{workspace.root}:{project['id']}:{source}", "task", title, occurrences[occurrence_key])
                occurrences[occurrence_key] += 1
                done = state in {"done", "completed", "complete", "完成", "已完成"}
                active = state in {"active", "in_progress", "进行中", "处理中"}
                yield {"title": title, "kind": "task", "project_id": project["id"], "project_root": str(workspace.root),
                       "status": "done" if done else "active" if active else "planned", "source": source}, identity


def project_task_signature(project_root: Path) -> dict[str, list[int] | None]:
    """Return a cheap signature so changed project task files trigger source sync."""
    workspace = ProjectWorkspace(project_root)
    signature = {}
    for project in workspace.registry()["projects"]:
        path = workspace.path(project["path"] + "/agent/TASKS.md")
        try:
            stat = path.stat()
        except OSError:
            signature[project["id"]] = None
        else:
            signature[project["id"]] = [stat.st_mtime_ns, stat.st_size]
    return signature


def migrate_legacy_tasks(store: Store, daily_root: Path, project_root: Path | None = None, context=None):
    """Import legacy source files once; never alter Markdown and allow new files to be imported later."""
    key = "legacy_task_import_v1"
    marker = store.setting(key, {})
    if not isinstance(marker, dict):
        marker = {}
    counts = {"daily": 0, "projects": 0, "errors": []}
    imported_refs = store.planned_task_source_refs()

    def import_entries(entries, category):
        for index, (values, source_ref) in enumerate(entries):
            if context:
                context.checkpoint()
            if source_ref in imported_refs:
                continue
            if store.add_planned_task(values, source_ref):
                counts[category] += 1
                imported_refs.add(source_ref)

    daily_key = str(daily_root.resolve())
    daily_root_changed = marker.get("daily") != daily_key
    daily_files = set(marker.get("daily_files", [])) if not daily_root_changed else set()
    new_daily_files = set(_daily_names(daily_root)) - daily_files
    if daily_root_changed or new_daily_files:
        import_entries(_daily_entries(daily_root, new_daily_files), "daily")
        marker["daily"] = daily_key
        marker["daily_files"] = sorted(daily_files | new_daily_files)
    if project_root:
        project_key = str(project_root.resolve())
        project_root_changed = marker.get("projects") != project_key
        imported_ids = set(marker.get("project_ids", [])) if not project_root_changed else set()
        workspace = ProjectWorkspace(project_root)
        project_records = workspace.registry()["projects"]
        current_ids = {project["id"] for project in project_records}
        new_ids = current_ids - imported_ids
        signature = project_task_signature(project_root)
        changed_files = marker.get("project_task_files") != signature
        projects_to_process = current_ids if project_root_changed or changed_files else new_ids
        for project_id in projects_to_process:
            entries = list(_project_entries(workspace, {project_id}))
            source_by_title = {values["title"]: values["source"] for values, _ in entries}
            default_source = "ai" if source_by_title and set(source_by_title.values()) == {"ai"} else None
            store.sync_legacy_project_task_sources(project_key, project_id, source_by_title, default_source)
            imported_refs.update(store.planned_task_source_refs())
            import_entries(entries, "projects")
        if project_root_changed or new_ids or changed_files:
            marker["projects"] = project_key
            marker["project_ids"] = sorted(imported_ids | new_ids)
            marker["project_task_files"] = signature
    store.set_setting(key, marker)
    return counts
