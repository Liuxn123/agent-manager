"""User-authored work summaries, stored with portable manager data."""
from pathlib import Path
from .domain import UserError
from .storage import now


def append_summary(root: Path, text: str) -> str:
    if not text.strip() or len(text) > 20000:
        raise UserError("请填写工作总结，最多 20000 字。")
    path = root / "work-summary.md"
    if path.is_symlink():
        raise UserError("总结文件不能是链接。")
    if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
        raise UserError("总结已超过 2 MiB，请先归档文件再继续。")
    previous = path.read_text(encoding="utf-8") if path.exists() else "# 我的工作总结\n"
    updated = previous.rstrip() + "\n\n## " + now() + "\n\n" + text.strip() + "\n"
    if len(updated.encode("utf-8")) > 2 * 1024 * 1024:
        raise UserError("总结已超过 2 MiB，请先归档文件再继续。")
    temporary = path.with_suffix(".tmp")
    if temporary.is_symlink():
        raise UserError("临时总结文件不能是链接。")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(path)
    return updated
