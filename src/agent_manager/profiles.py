"""Agent directory presets. Discovery lists paths; it never reads conversations."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .domain import Resource, UserError

ENGINES = ["Codex", "WorkBuddy", "CodeBuddy", "Claude Code", "其他 Agent"]
RECORD_NOTE = "保存选定目录中的本地记录、配置和附件。云端记录需先由原应用导出；换电脑后原应用可能需要重新登录和重新登记项目。"


def discover_record_paths(engine: str) -> list[Path]:
    home = Path.home()
    if engine == "Codex":
        candidates = [Path(os.environ.get("CODEX_HOME", str(home / ".codex")))]
        if os.environ.get("CODEX_SQLITE_HOME"):
            candidates.append(Path(os.environ["CODEX_SQLITE_HOME"]))
    elif engine == "WorkBuddy":
        candidates = [home / ".workbuddy"]
        if sys.platform == "win32":
            candidates += [Path(os.environ.get(key, str(home / "AppData" / folder))) / "CodeBuddyExtension"
                           for key, folder in [("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local")]]
        elif sys.platform == "darwin":
            candidates += [home / "Library/Application Support/CodeBuddyExtension"]
        else:
            candidates += [home / ".config/CodeBuddyExtension"]
    elif engine == "CodeBuddy":
        candidates = [home / ".codebuddy"]
    elif engine == "Claude Code":
        candidates = [home / ".claude"]
    else:
        candidates = []
    result = []
    for path in candidates:
        if path.is_dir() and not path.is_symlink():
            path = path.resolve()
            if not any(path == previous or path.is_relative_to(previous) for previous in result):
                result.append(path)
    return result


def sources_for(resource: Resource) -> list[dict]:
    """Stable bundle component names are independent of the source computer."""
    if resource.kind != "agent" or not resource.options.get("portable_bundle"):
        return [{"id": "root", "label": "项目文件", "role": "project", "path": str(resource.options.get("path", "")), "prefix": ""}]
    result = []
    if resource.options.get("path"):
        result.append({"id": "project", "label": "项目文件", "role": "project", "path": resource.options["path"], "prefix": "project"})
    paths = resource.options.get("record_paths", [])
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths) or len(paths) > 20:
        raise UserError("本地记录目录配置不正确；最多添加 20 个目录。")
    for index, path in enumerate(paths):
        key = "records" if index == 0 else f"records{index + 1}"
        result.append({"id": key, "label": "本地记录" if index == 0 else f"本地记录 {index + 1}", "role": "records", "path": path, "prefix": key})
    if not result:
        raise UserError("请至少选择一个项目目录或本地记录目录。")
    return result


def restored_resource(manifest: dict, target: Path, folders: dict[str, str]) -> Resource:
    components = manifest.get("components", [])
    options = {"path": str(target)}
    if components:
        options = {"path": "", "engine": manifest.get("engine", "其他 Agent"), "portable_bundle": True, "record_paths": []}
        for component in components:
            path = str(target / folders.get(component["id"], component["prefix"]))
            if component["role"] == "project":
                options["path"] = path
            else:
                options["record_paths"].append(path)
    # Restored data must never automatically launch programs from the old host.
    return Resource.from_dict({"name": manifest.get("resource_name", "导入的资料"), "kind": manifest["kind"],
                               "id": manifest["resource_id"], "options": options})
