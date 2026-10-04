"""Open the existing project files through Obsidian's standard URI protocol."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlencode

from .domain import UserError
from .project_workspaces import ProjectWorkspace, MANAGEMENT_FILES, linked


def open_uri(path: Path) -> str:
    # Keep the lexical junction path: resolving it would select another vault.
    return "obsidian://open?" + urlencode({"path": str(path), "paneType": "tab"}, quote_via=quote)


def require_vault(path: Path) -> None:
    if not path.is_dir() or not (path / ".obsidian").is_dir():
        raise UserError("请先在 Obsidian 中把这个文件夹作为知识库打开一次：" + str(path))
    if linked(path) or linked(path / ".obsidian"):
        raise UserError("知识库根目录和 .obsidian 配置不能使用联接或符号链接。")


def project_uri(workspace: ProjectWorkspace, identity: str, relative: str) -> dict:
    item = workspace.project(identity)
    if relative not in MANAGEMENT_FILES and not (relative.startswith("笔记/") and len(Path(relative).parts) == 2 and relative.lower().endswith(".md")):
        raise UserError("请选择项目管理文档或项目笔记。")
    source = workspace.path(item["path"] + "/" + relative)
    if not source.is_file():
        raise UserError("文档已移动或不存在，请刷新项目。")
    if item.get("vault_entry"):
        entry = workspace.path(item["vault_entry"], allow_link=True)
        target = workspace.path(item["path"])
        if not linked(entry) or not entry.is_dir() or entry.resolve() != target.resolve():
            raise UserError("Obsidian 项目入口缺失或指向其他目录，请先核对入口；不会打开错误的项目。")
        vault = workspace.path("myself")
        require_vault(vault)
        path = entry / relative
    else:
        # Honor projects whose registered policy intentionally excludes a vault entry.
        vault = workspace.path(item["path"])
        if not (vault / ".obsidian").is_dir():
            raise UserError("这个项目按登记策略没有 Obsidian 入口。可用“打开文件夹”；若要单独使用 Obsidian，请先把真实项目目录作为知识库打开。")
        require_vault(vault)
        path = source
    return {"uri": open_uri(path), "path": str(path), "vault": str(vault)}


def index_uri(workspace: ProjectWorkspace, archived: bool = False) -> dict:
    vault = workspace.path("myself")
    require_vault(vault)
    path = workspace.path("myself/03-项目/" + ("归档项目索引.md" if archived else "项目总览.md"))
    if not path.is_file():
        raise UserError("项目索引不存在，请先刷新 Obsidian 索引。")
    return {"uri": open_uri(path), "path": str(path), "vault": str(vault)}
