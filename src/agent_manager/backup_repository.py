"""Mirror verified encrypted Agent archives; never export plaintext records or keys."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from . import archives
from .domain import UserError
from .profiles import sources_for
from .runtime import TaskContext
from .storage import Store

MAX_GIT_ARCHIVE = 50 * 1024 * 1024
MARKER = {"schema_version": 1, "type": "agent-manager-encrypted-backups"}
ENGINES = {"Codex": "codex", "WorkBuddy": "workbuddy", "CodeBuddy": "codebuddy", "Claude Code": "claude-code"}
ARCHIVE_PATH = re.compile(r"archives/(codex|workbuddy|codebuddy|claude-code|other)/[a-f0-9]{32}/[a-f0-9]{32}\.amb\Z")
README = """# Agent 加密备份仓库

这里集中保存 Codex、WorkBuddy、CodeBuddy、Claude Code 等工作 Agent 的已校验加密备份副本。
myself 使用自己的仓库；普通项目不检查或打包 Git 历史；Hermes 使用原有备份仓库。

## 使用

1. 在 Agent 管家中登记 Agent 记录目录，退出对应 Agent 后执行“立即备份”。
2. 在“备份与换电脑”选择本仓库，点击“整理 Agent 备份”。备份口令不存入仓库。
3. 整理只更新本地仓库副本，不自动提交或推送。使用你现有的 Git 工具提交本仓库并推送到私有远端。
4. 换电脑后克隆本仓库，在管家点击“从备份文件恢复”，选择 archives 下的 .amb，输入口令，恢复到新目录。

catalog.json 只包含 Agent 类型、资源编号、相对路径、时间、大小与 SHA-256，不包含本机路径或聊天正文。
单个备份限 50 MiB；较大备份仍保留在管家的便携 backups 目录，可直接用 U 盘携带。
整理不会删除便携备份或仓库旧版本。Git 仓库不是大型备份存储，长期版本需要自行整理。
不要加入原始记录、登录凭据、密钥、备份口令或个人资源配置。
"""


def _regular(path: Path) -> bool:
    return path.is_file() and not any(archives.is_link(item) for item in (path, *path.parents))


def _digest(path: Path, context: TaskContext) -> str:
    if not _regular(path):
        raise UserError("仓库或备份含有目录联接、链接或非普通文件，已停止整理。")
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        for chunk in iter(lambda: reader.read(archives.CHUNK), b""):
            context.checkpoint()
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise UserError("备份在整理期间变化，请重试。")
    return digest.hexdigest()


def _write_json(path: Path, value: dict) -> None:
    if path.exists() and not _regular(path):
        raise UserError("仓库索引不是普通文件，已停止整理。")
    temporary = path.with_name(path.name + "." + uuid4().hex + ".partial")
    try:
        with temporary.open("x", encoding="utf-8") as writer:
            json.dump(value, writer, ensure_ascii=False, indent=2)
            writer.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def initialize(repository: Path) -> Path:
    if any(archives.is_link(path) for path in (repository, *repository.parents)):
        raise UserError("专用备份仓库不能使用目录联接或符号链接。")
    repository = repository.expanduser().resolve()
    marker = repository / "repository.json"
    if marker.exists():
        if not _regular(marker) or marker.stat().st_size > 1000 or json.loads(marker.read_text(encoding="utf-8")) != MARKER:
            raise UserError("这不是受支持的 Agent 加密备份仓库。")
    else:
        if repository.exists() and any(repository.iterdir()):
            raise UserError("请选择专用 Agent 备份仓库或空文件夹。不能把 myself、项目或旧 workbench 仓库直接选在这里。")
        repository.mkdir(parents=True, exist_ok=True)
        (repository / "README.md").write_text(README, encoding="utf-8")
        (repository / ".gitignore").write_text("*.partial\n.env*\nauth.json\n*.key\n*.pem\n*.sqlite*\n*.db\ndata/\nrestored/\n", encoding="utf-8")
        _write_json(marker, MARKER)
    return repository


def _catalog(repository: Path, context: TaskContext) -> dict[str, dict]:
    path = repository / "catalog.json"
    if not path.exists():
        return {}
    if not _regular(path) or path.stat().st_size > 5_000_000:
        raise UserError("备份仓库索引不安全或过大。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("archives"), list):
        raise UserError("备份仓库索引格式不正确。")
    rows = {}
    for row in data["archives"]:
        context.checkpoint()
        if not isinstance(row, dict) or set(row) != {"path", "resource_id", "engine", "created_at", "size", "sha256"}:
            raise UserError("备份仓库索引含有不支持的字段。")
        relative = row["path"]
        if not isinstance(relative, str) or not ARCHIVE_PATH.fullmatch(relative) or relative in rows:
            raise UserError("备份仓库索引路径不正确或重复。")
        source = repository / relative
        if not _regular(source) or not isinstance(row["size"], int) or not 0 < row["size"] <= MAX_GIT_ARCHIVE:
            raise UserError("仓库中的备份缺失、超限或不是普通文件。")
        if source.stat().st_size != row["size"] or _digest(source, context) != row["sha256"]:
            raise UserError("仓库中的备份已改变，已停止整理；原副本保留。")
        if row["resource_id"] != relative.split("/")[2] or row["engine"] != relative.split("/")[1]:
            raise UserError("备份仓库索引归属不正确。")
        if not isinstance(row["created_at"], str) or len(row["created_at"]) > 40:
            raise UserError("备份仓库时间字段不正确。")
        rows[relative] = row
    return rows


def organize(store: Store, backups: Path, repository: Path, context: TaskContext) -> dict:
    agents = [resource for resource in store.resources() if resource.kind == "agent"]
    target = repository.expanduser().resolve()
    for resource in agents:
        for component in sources_for(resource):
            if component["path"] and target.is_relative_to(Path(component["path"]).expanduser().resolve()):
                raise UserError("备份仓库不能放在被备份的 Agent 原始资料目录里面。")
    if target == backups.resolve() or target.is_relative_to(backups.resolve()):
        raise UserError("专用仓库请放在便携 backups 目录之外。")
    repository = initialize(repository)
    rows = _catalog(repository, context)
    copied = existing = skipped = 0
    for resource in agents:
        if not re.fullmatch(r"[a-f0-9]{32}", resource.id):
            raise UserError("Agent 资源编号格式不正确。")
        for metadata in archives.list_archives(backups, resource.id):
            context.checkpoint()
            source = Path(metadata["archive"])
            evidence = store.evidence(str(source.resolve()))
            if not re.fullmatch(r"[a-f0-9]{32}\.amb", source.name) or not _regular(source):
                skipped += 1
                continue
            if (not evidence.get("verified_at") or evidence.get("kind") != "agent"
                    or evidence.get("resource_id") != resource.id or not 0 < source.stat().st_size <= MAX_GIT_ARCHIVE):
                skipped += 1
                continue
            with source.open("rb") as reader:
                encrypted = reader.read(len(archives.MAGIC)) == archives.MAGIC
            digest = _digest(source, context)
            if not encrypted or digest != evidence.get("archive_sha256"):
                skipped += 1
                continue
            engine = ENGINES.get(evidence.get("engine"), "other")
            relative = f"archives/{engine}/{resource.id}/{source.name}"
            destination = repository / relative
            if any(archives.is_link(path) for path in (destination, *destination.parents)):
                raise UserError("仓库目标存在链接，已停止整理。")
            if destination.exists():
                if _digest(destination, context) != digest:
                    raise UserError("仓库已有同名但不同内容的备份，已保留原文件并停止整理。")
                existing += 1
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + "." + uuid4().hex + ".partial")
                try:
                    with source.open("rb") as reader, temporary.open("xb") as writer:
                        while chunk := reader.read(archives.CHUNK):
                            context.checkpoint()
                            writer.write(chunk)
                    if _digest(temporary, context) != digest or _digest(source, context) != digest:
                        raise UserError("备份在复制期间变化，已停止整理。")
                    # Exclusive creation protects against a competing external writer.
                    created = False
                    try:
                        with temporary.open("rb") as reader, destination.open("xb") as writer:
                            created = True
                            while chunk := reader.read(archives.CHUNK):
                                context.checkpoint()
                                writer.write(chunk)
                        if _digest(destination, context) != digest:
                            raise UserError("仓库副本校验失败，已停止整理。")
                    except BaseException:
                        if created:
                            destination.unlink(missing_ok=True)
                        raise
                    copied += 1
                finally:
                    temporary.unlink(missing_ok=True)
            rows[relative] = {"path": relative, "resource_id": resource.id, "engine": engine,
                              "created_at": evidence["created_at"], "size": destination.stat().st_size, "sha256": digest}
    _write_json(repository / "catalog.json", {"schema_version": 1, "archives": sorted(rows.values(), key=lambda row: row["path"])})
    note = "这里只整理本地仓库副本；使用现有 Git 工具提交并推送私有远端。U 盘备份保留。"
    if skipped:
        note += "未校验、被修改或超过 50 MiB 的备份已跳过，请先校验或直接用 U 盘携带。"
    if not rows:
        note += "当前尚无 Agent 备份：先在“其他 Agent”执行立即备份，再整理。"
    return {"copied": copied, "existing": existing, "skipped": skipped, "total": len(rows), "note": note}
