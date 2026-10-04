from __future__ import annotations

from pathlib import Path

from ..domain import Resource, UserError
from ..runtime import TaskContext, run_process
from .local import required_directory


class ProjectAdapter:
    capabilities = frozenset({"observe", "backup", "verify", "restore", "open", "git_pull", "open_vault"})

    def observe(self, resource: Resource, context: TaskContext) -> dict:
        root = required_directory(resource.options, "path")
        context.checkpoint()
        result = {"path": str(root), "type": "Obsidian Vault" if resource.kind == "vault" else "项目", "entries": []}
        for path in sorted(root.iterdir(), key=lambda path: (not path.is_dir(), path.name.casefold())):
            context.checkpoint()
            if len(result["entries"]) >= 100:
                break
            result["entries"].append({"name": path.name, "type": "目录" if path.is_dir() else "文件", "link": path.is_symlink()})
        if (root / ".git").exists():
            result["git_status"] = run_process(["git", "-C", str(root), "status", "--short", "--branch"], context, timeout=30).strip()
            result["remotes"] = run_process(["git", "-C", str(root), "remote"], context, timeout=30).splitlines()
        else:
            result["git_status"] = "未登记 Git 仓库"
        result["backup_note"] = "加密保存工作文件、附件及本地 Git 历史；依赖目录默认排除。关闭写入程序后备份。"
        return result

    def git_pull(self, resource: Resource, context: TaskContext) -> dict:
        root = required_directory(resource.options, "path")
        if not (root / ".git").exists():
            raise UserError("此项目不是 Git 仓库。")
        if run_process(["git", "-C", str(root), "status", "--porcelain"], context, timeout=30).strip():
            raise UserError("项目有未提交或未跟踪文件，请先保存，避免拉取时混合修改。")
        output = run_process(["git", "-C", str(root), "pull", "--ff-only"], context, timeout=180, cancellable=False)
        return {"pulled": True, "result": output.strip()}
