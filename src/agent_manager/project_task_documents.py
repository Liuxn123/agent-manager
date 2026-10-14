"""Sync separate, editable AI/manual project task Markdown documents with SQLite."""
from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

from .domain import UserError
from .project_workspaces import ProjectWorkspace, lifecycle_state
from .storage import Store


SOURCES = {
    "ai": ("agent/TASKS-AI.md", "AI 规划任务"),
    "manual": ("agent/TASKS-MANUAL.md", "手动编写任务"),
}
START = "<!-- agent-manager:tasks:start -->"
END = "<!-- agent-manager:tasks:end -->"
HEADER = "| ID | 任务 | 状态 | 日期 | 优先级 | 类型 | 目标编号 | 备注 | 完成记录 |\n|---|---|---|---|---|---|---|---|---|"
FIELDS = ("id", "title", "status", "due_date", "priority", "kind", "goal_id", "notes", "completion_note")
LEGACY_FIELDS = FIELDS[:-1]


def _split_row(line: str) -> list[str]:
    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    cells, current = [], []
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _cell(value) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>") or "-"


def _plain(value: str) -> str:
    return value.replace("<br>", "\n").replace("\\|", "|").replace("\\\\", "\\") if value != "-" else ""


def _block(text: str) -> tuple[str, str, str]:
    if text.count(START) > 1 or text.count(END) > 1:
        raise UserError("任务文档包含重复的自动管理区块标记，已停止同步以保护原文。")
    start = text.find(START)
    end = text.find(END)
    if start < 0 and end < 0:
        return text, "", ""
    if start < 0 or end < start:
        raise UserError("任务文档的自动管理区块标记不完整，已停止同步以保护原文。")
    return text[:start], text[start + len(START):end], text[end + len(END):]


def _parse_rows(body: str, source: str, identity: str, root: Path) -> list[dict]:
    rows = []
    in_table = False
    for line in body.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = _split_row(line)
        if cells and cells[0].casefold() == "id":
            in_table = True
            continue
        if not in_table or len(cells) not in {len(LEGACY_FIELDS), len(FIELDS)} or all(
                re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        fields = FIELDS if len(cells) == len(FIELDS) else LEGACY_FIELDS
        raw = dict(zip(fields, cells))
        task_id = _plain(raw["id"])
        if task_id.casefold() in {"", "-", "new"}:
            task_id = ""
        rows.append({
            "id": task_id,
            "title": _plain(raw["title"]),
            "kind": _plain(raw["kind"]) or "task",
            "due_date": _plain(raw["due_date"]) or None,
            "project_id": identity,
            "project_root": str(root.resolve()),
            "goal_id": _plain(raw["goal_id"]) or None,
            "priority": _plain(raw["priority"]),
            "status": _plain(raw["status"]) or "planned",
            "source": source,
            "notes": _plain(raw["notes"]),
            "completion_note": _plain(raw.get("completion_note", "")),
        })
    return rows


def _render(source: str, tasks: list[dict]) -> str:
    lines = [START, "", HEADER]
    for task in tasks:
        lines.append("| " + " | ".join(_cell(task.get(field)) for field in FIELDS) + " |")
    lines.extend([END])
    return "\n".join(lines)


def _export_locked(store: Store, workspace: ProjectWorkspace, identity: str, context=None) -> list[str]:
    project = workspace.project(identity)
    changed = []
    for source, (relative, title) in SOURCES.items():
        if context:
            context.checkpoint()
        path = workspace.path(project["path"] + "/" + relative)
        original = workspace.read(path) if path.exists() else None
        if original is None:
            status_note = ("任务完成状态与完成记录由项目概览中的 Codex 执行结果维护；请勿手动改成已完成。" if source == "ai"
                           else "修改标题、日期或状态后，在项目「任务」页点击“同步任务文档”。")
            before, after = (f"# {title}\n\n在下方表格编辑任务。{status_note}新任务将 ID 填为 `new`；请勿重复或改动已有 ID。\n\n删除表格行不会删除任务，数据库中的任务会在同步时恢复到表格。\n\n", "\n")
        elif START not in original and END not in original:
            before, after = (original.rstrip() + "\n\n", "\n")
        else:
            before, _body, after = _block(original)
        tasks = [task for task in store.planned_tasks(project_id=identity, project_root=str(workspace.root.resolve()))
                 if task.get("source") == source]
        result = before + _render(source, tasks) + after
        if result != original:
            workspace.write(path, result, original)
            changed.append(str(path))
    return changed


def sync_project_task_documents(store: Store, workspace: ProjectWorkspace, identity: str, context=None) -> dict:
    """Import edits from each source document, then refresh managed tables from SQLite.

    Text outside the marked table is preserved. Missing rows do not delete tasks;
    deletion remains a deliberate database operation rather than an accidental edit.
    """
    project = workspace.project(identity)
    status_path = workspace.path(project["path"] + "/agent/STATUS.md")
    if lifecycle_state(workspace.read(status_path)) == "archived":
        raise UserError("归档项目为只读，请先重新启用。")
    imported = 0
    parsed_rows = []
    seen_ids: dict[str, str] = {}
    with workspace.lock("SyncProjectTaskDocuments"):
        for source, (relative, title) in SOURCES.items():
            if context:
                context.checkpoint()
            path = workspace.path(project["path"] + "/" + relative)
            original = workspace.read(path) if path.exists() else None
            if original is None:
                before, body, after = (f"# {title}\n\n在下方表格编辑任务。修改标题、日期或状态后，在项目「任务」页点击“同步任务文档”。新任务将 ID 填为 `new`；请勿重复或改动已有 ID。\n\n删除表格行不会删除任务，数据库中的任务会在同步时恢复到表格。\n\n", "", "\n")
            else:
                before, body, after = _block(original)
            rows = _parse_rows(body, source, identity, workspace.root)
            for values in rows:
                if not values["title"]:
                    raise UserError(f"{relative} 中存在空任务名称，请补充后再同步。")
                task_id = values.pop("id")
                if task_id and not re.fullmatch(r"[a-fA-F0-9]{32}", task_id):
                    raise UserError(f"{relative} 中任务 ID 格式无效：{task_id}")
                if task_id and task_id in seen_ids:
                    other = seen_ids[task_id]
                    raise UserError(f"任务 ID {task_id} 在 {other} 与 {relative} 中重复，未同步。")
                if task_id:
                    seen_ids[task_id] = relative
                    existing = store.planned_task(task_id)
                else:
                    existing = None
                if existing:
                    if existing.get("project_id") != identity or existing.get("project_root") != str(workspace.root.resolve()):
                        raise UserError(f"任务 ID {task_id} 不属于当前项目，未同步。")
                if (source == "ai" and values.get("status") == "done"
                        and (not existing or existing.get("status") != "done")):
                    raise UserError("AI 规划任务必须由 Agent 执行并验证后才能完成；请恢复该文档中的状态，再使用项目概览的 Codex 执行入口。")
                store._validate_planned_task(values)
                parsed_rows.append((task_id, source, values, existing))
        for task_id, source, values, existing in parsed_rows:
            if existing:
                store.update_planned_task(task_id, values, sync_documents=False)
            else:
                created = store.add_planned_task(
                    values, source_ref=f"project-doc:{workspace.root.resolve()}:{identity}:{source}:{uuid4().hex}",
                    sync_documents=False)
                if created:
                    imported += 1
        exported = _export_locked(store, workspace, identity, context)
    return {"project_id": identity, "imported": imported, "updated": exported,
            "paths": [workspace.path(project["path"] + "/" + relative) for relative, _ in SOURCES.values()]}


def export_project_task_documents(store: Store, workspace: ProjectWorkspace, identity: str, context=None) -> dict:
    """Refresh the generated task tables, preserving all text around them."""
    project = workspace.project(identity)
    if lifecycle_state(workspace.read(workspace.path(project["path"] + "/agent/STATUS.md"))) == "archived":
        return {"project_id": identity, "paths": []}
    with workspace.lock("ExportProjectTaskDocuments"):
        return {"project_id": identity, "paths": _export_locked(store, workspace, identity, context)}
