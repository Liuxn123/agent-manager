from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .domain import Resource, RestorePlan, UserError
from .runtime import TaskContext, run_process
from .storage import now

MAGIC = b"AGENT-MANAGER-1\n"
CHUNK = 1024 * 1024
MAX_SIZE = 50 * 1024**3
MAX_FILES = 200_000
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".cache", ".pytest_cache"}


def key_for(password: str, salt: bytes) -> bytes:
    if not password:
        raise UserError("备份口令不能为空。")
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password.encode("utf-8"))


class EncryptWriter(io.RawIOBase):
    """Let zipfile stream directly into AES-GCM; no plaintext ZIP on disk."""
    def __init__(self, handle, encryptor) -> None:
        self.handle = handle
        self.encryptor = encryptor
        self.position = 0

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def tell(self) -> int:
        return self.position

    def write(self, data: bytes) -> int:
        self.handle.write(self.encryptor.update(data))
        self.position += len(data)
        return len(data)

    def flush(self) -> None:
        self.handle.flush()


def path_token(source: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/")):
        raise UserError("备份包含不安全的文件路径。")
    if any(":" in part or part.endswith((".", " ")) or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))} for part in path.parts):
        raise UserError("备份包含无法跨平台恢复的文件名。")
    return str(path)


def create_archive(resource: Resource, destination: Path, password: str, context: TaskContext) -> dict:
    source = Path(resource.options.get("path", "")).expanduser()
    if not source.is_dir() or is_link(source):
        raise UserError("请选择真实存在的项目目录，不能使用符号链接作为根目录。")
    source = source.resolve()
    destination = destination.expanduser().resolve()
    if destination == source or destination.is_relative_to(source):
        raise UserError("备份目录必须位于被备份目录之外。")
    destination.mkdir(parents=True, exist_ok=True)
    identity = uuid4().hex
    final = destination / f"{identity}.amb"
    temporary = destination / f".{identity}.partial"
    records: list[dict] = []
    excluded: list[str] = []
    directories: list[str] = []
    total = 0
    extra_excludes = set(resource.options.get("exclude_dirs", []))
    salt, nonce = os.urandom(16), os.urandom(12)
    encryptor = Cipher(algorithms.AES(key_for(password, salt)), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(MAGIC)
    context.log("正在创建加密备份，明文 ZIP 不会写入磁盘。")
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(MAGIC + salt + nonce)
            with zipfile.ZipFile(EncryptWriter(handle, encryptor), "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for root, dirs, files in os.walk(source, followlinks=False):
                    context.checkpoint()
                    current = Path(root)
                    retained = []
                    for name in dirs:
                        path = current / name
                        relative = path.relative_to(source).as_posix()
                        if name in SKIP_DIRS | extra_excludes or is_link(path):
                            excluded.append(relative + "/")
                        else:
                            retained.append(name)
                            directories.append(safe_name(relative))
                    dirs[:] = retained
                    for name in sorted(files):
                        context.checkpoint()
                        path = current / name
                        relative = path.relative_to(source).as_posix()
                        if is_link(path):
                            excluded.append(relative)
                            continue
                        if not path.is_file():
                            raise UserError("目录包含特殊文件，无法创建一致备份。")
                        safe_name(relative)
                        before = path.stat()
                        total += before.st_size
                        if total > MAX_SIZE or len(records) >= MAX_FILES:
                            raise UserError("备份超出首版容量限制（50 GiB / 20 万文件）。")
                        digest = hashlib.sha256()
                        with path.open("rb") as reader, archive.open("files/" + relative, "w", force_zip64=True) as writer:
                            for chunk in iter(lambda: reader.read(CHUNK), b""):
                                context.checkpoint()
                                digest.update(chunk)
                                writer.write(chunk)
                        after = path.stat()
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise UserError("备份期间文件发生变化，请关闭写入程序后重试。")
                        records.append({"path": relative, "sha256": digest.hexdigest(), "size": before.st_size, "mode": stat.S_IMODE(before.st_mode)})
                # Bundle preserves local commits without copying a live .git directory.
                if (source / ".git").exists():
                    with tempfile.TemporaryDirectory(prefix="agent-manager-git-") as temporary_git:
                        bundle = Path(temporary_git) / "history.bundle"
                        try:
                            heads = subprocess.run(["git", "-C", str(source), "show-ref"], capture_output=True, timeout=30,
                                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                        except (OSError, subprocess.TimeoutExpired) as exc:
                            raise UserError("无法检查 Git 历史，请检查 Git 安装。") from exc
                        if heads.returncode not in {0, 1}:
                            raise UserError("Git 历史检查失败，备份未完成。")
                        if heads.returncode == 0:
                            if any(record["path"].casefold() == ".agent-manager-history.bundle" for record in records):
                                raise UserError("项目含有保留文件 .agent-manager-history.bundle，请先移动该历史资料包。")
                            run_process(["git", "-C", str(source), "bundle", "create", str(bundle), "--all"], context)
                            archive.write(bundle, "git/history.bundle")
                            records.append({"path": "@git-bundle", "sha256": path_token(bundle), "size": bundle.stat().st_size, "mode": 0o600})
                manifest = {"schema_version": 1, "resource_id": resource.id, "kind": resource.kind,
                            "created_at": now(), "files": records, "directories": directories, "excluded": excluded}
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            handle.write(encryptor.finalize())
            handle.write(encryptor.tag)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(final)
        metadata = {"id": identity, "resource_id": resource.id, "kind": resource.kind, "created_at": manifest["created_at"],
                    "file_count": len(records), "size": final.stat().st_size, "excluded_count": len(excluded), "encrypted": True}
        final.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        context.log(f"加密备份已完成：{len(records)} 个文件，排除 {len(excluded)} 项。")
        return {**metadata, "archive": str(final), "excluded": excluded[:100], "verified": False}
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def open_archive(source: Path, password: str, context: TaskContext, expected_sha256: str = "") -> Iterator[zipfile.ZipFile]:
    if not source.is_file() or is_link(source):
        raise UserError("备份文件不存在或是链接。")
    minimum = len(MAGIC) + 16 + 12 + 16
    if source.stat().st_size < minimum or source.stat().st_size > MAX_SIZE + 1024**3:
        raise UserError("备份文件大小不正确。")
    with source.open("rb") as handle, tempfile.TemporaryFile() as decrypted:
        magic = handle.read(len(MAGIC))
        if magic != MAGIC:
            raise UserError("不支持的加密备份格式。")
        salt, nonce = handle.read(16), handle.read(12)
        content_digest = hashlib.sha256(magic + salt + nonce)
        handle.seek(-16, os.SEEK_END)
        tag = handle.read(16)
        decryptor = Cipher(algorithms.AES(key_for(password, salt)), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(MAGIC)
        handle.seek(len(MAGIC) + 28)
        remaining = source.stat().st_size - minimum
        while remaining:
            context.checkpoint()
            chunk = handle.read(min(CHUNK, remaining))
            if not chunk:
                raise UserError("加密备份被截断。")
            remaining -= len(chunk)
            content_digest.update(chunk)
            decrypted.write(decryptor.update(chunk))
        content_digest.update(tag)
        try:
            decrypted.write(decryptor.finalize())
        except InvalidTag as exc:
            raise UserError("口令错误或备份被篡改，目标目录未修改。") from exc
        source_sha256 = content_digest.hexdigest()
        if expected_sha256 and expected_sha256 != source_sha256:
            raise UserError("备份在预览后发生变化，请重新预览。")
        decrypted.seek(0)
        try:
            with zipfile.ZipFile(decrypted) as archive:
                archive.source_sha256 = source_sha256
                yield archive
        except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UserError("备份内部格式损坏，目标目录未修改。") from exc


def verify_open(archive: zipfile.ZipFile, context: TaskContext) -> dict:
    infos = archive.infolist()
    names = [entry.filename for entry in infos]
    if len(names) > MAX_FILES + 1 or len(names) != len(set(names)):
        raise UserError("备份文件数量异常或有重复条目。")
    if sum(entry.file_size for entry in infos) > MAX_SIZE:
        raise UserError("备份展开大小超出限制。")
    for entry in infos:
        safe_name(entry.filename)
        if stat.S_ISLNK(entry.external_attr >> 16):
            raise UserError("备份包含符号链接，已拒绝恢复。")
    if archive.getinfo("manifest.json").file_size > 32_000_000:
        raise UserError("备份清单过大。")
    manifest = json.loads(archive.read("manifest.json"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list):
        raise UserError("备份清单版本或内容不正确。")
    expected = {"manifest.json"}
    folded: set[str] = set()
    for record in manifest["files"]:
        relative = record["path"]
        name = "git/history.bundle" if relative == "@git-bundle" else "files/" + safe_name(relative)
        if name in expected or name.casefold() in folded:
            raise UserError("备份路径存在重复或跨平台大小写冲突。")
        expected.add(name)
        folded.add(name.casefold())
        if archive.getinfo(name).file_size != record["size"]:
            raise UserError("文件大小与清单不符。")
        digest = hashlib.sha256()
        with archive.open(name) as reader:
            for chunk in iter(lambda: reader.read(CHUNK), b""):
                context.checkpoint()
                digest.update(chunk)
        if digest.hexdigest() != record["sha256"]:
            raise UserError("文件哈希与清单不符。")
    if set(names) != expected:
        raise UserError("备份含有未声明文件。")
    for directory in manifest.get("directories", []):
        safe_name(directory)
    return manifest


def verify_archive(source: Path, password: str, context: TaskContext) -> dict:
    with open_archive(source, password, context) as archive:
        manifest = verify_open(archive, context)
        source_sha256 = archive.source_sha256
    return {"valid": True, "file_count": len(manifest["files"]), "resource_id": manifest["resource_id"],
            "created_at": manifest["created_at"], "excluded": manifest.get("excluded", [])[:100], "archive_sha256": source_sha256}


def plan_restore(resource: Resource, source: Path, target: Path, password: str, context: TaskContext) -> RestorePlan:
    target = target.expanduser().absolute()
    target = check_target(target, source)
    report = verify_archive(source, password, context)
    if report["resource_id"] != resource.id:
        raise UserError("此备份属于其他资源，请在对应资源下恢复。")
    return RestorePlan(resource.id, report["archive_sha256"], str(source.resolve()), str(target),
                       {**report, "target": str(target), "existing_files_overwritten": 0,
                        "note": "恢复到空目录；Git 历史保存在 .agent-manager-history.bundle，可手动克隆恢复仓库。"})


def canonical_system_path(target: Path) -> Path:
    target = target.expanduser().absolute()
    # macOS exposes immutable OS aliases /var, /tmp, /etc. Normalize only these
    # exact well-known mappings; user-controlled symlinks remain forbidden.
    if sys.platform == "darwin":
        for name in ("var", "tmp", "etc"):
            alias, actual = Path("/" + name), Path("/private/" + name)
            if target.is_relative_to(alias) and alias.is_symlink() and alias.resolve() == actual:
                target = actual / target.relative_to(alias)
                break
    return target


def check_target(target: Path, source: Path) -> Path:
    target = canonical_system_path(target)
    if is_link(target) or any(is_link(parent) for parent in target.parents):
        raise UserError("恢复目标不能包含符号链接或目录联接。")
    resolved = target.resolve()
    if resolved == Path(resolved.anchor) or source.resolve().is_relative_to(resolved):
        raise UserError("恢复目标不能是磁盘根目录或包含备份文件。")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise UserError("首版只恢复到新目录或空目录，请选择其他目标。")
    return target


def apply_restore(plan: RestorePlan, password: str, context: TaskContext) -> dict:
    source, target = Path(plan.source), Path(plan.target)
    check_target(target, source)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".agent-manager-restore-", dir=target.parent))
    try:
        with open_archive(source, password, context, expected_sha256=plan.token) as archive:
            manifest = verify_open(archive, context)
            if manifest["resource_id"] != plan.resource_id:
                raise UserError("备份资源身份发生变化。")
            required = sum(record["size"] for record in manifest["files"])
            if shutil.disk_usage(staging).free < required + 64 * 1024**2:
                raise UserError("目标磁盘空间不足。")
            for directory in manifest.get("directories", []):
                (staging / safe_name(directory)).mkdir(parents=True, exist_ok=True)
            for record in manifest["files"]:
                context.checkpoint()
                relative = record["path"]
                name = "git/history.bundle" if relative == "@git-bundle" else "files/" + safe_name(relative)
                destination = staging / (".agent-manager-history.bundle" if relative == "@git-bundle" else relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as reader, destination.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, CHUNK)
                # Restore ordinary permissions only; never set setuid/setgid bits.
                os.chmod(destination, int(record.get("mode", 0o600)) & 0o777)
        context.checkpoint()
        check_target(target, source)
        context.log("校验通过，正在切换恢复目录；此步骤不强制取消。")
        if target.exists():
            target.rmdir()
        staging.replace(target)
        return {"applied": True, "target": str(target), "file_count": len(manifest["files"]),
                "excluded": manifest.get("excluded", [])[:100], "git_history": any(r["path"] == "@git-bundle" for r in manifest["files"])}
    finally:
        # Only the internally created and resolved staging directory is removed.
        if staging.exists() and staging.parent.resolve() == target.parent.resolve() and staging.name.startswith(".agent-manager-restore-"):
            shutil.rmtree(staging)


def list_archives(root: Path, resource_id: str) -> list[dict]:
    if not root.is_dir():
        return []
    result = []
    for source in root.glob("*.amb"):
        if source.is_symlink():
            continue
        sidecar = source.with_suffix(".json")
        try:
            if sidecar.stat().st_size > 20_000:
                continue
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(metadata, dict) and metadata.get("resource_id") == resource_id:
                result.append({**metadata, "archive": str(source), "verified": False})
        except (OSError, ValueError):
            continue
    return sorted(result, key=lambda item: item.get("created_at", ""), reverse=True)
