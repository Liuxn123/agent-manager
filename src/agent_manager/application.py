from __future__ import annotations

import shutil
import os
import sys
import tempfile
import hashlib
from pathlib import Path

from . import archives
from .adapters.agents import AgentAdapter
from .adapters.local import LocalHermesAdapter
from .adapters.projects import ProjectAdapter, VaultAdapter
from .adapters.server import ServerHermesAdapter
from .domain import AdapterRegistry, Resource, RestorePlan, UserError
from .runtime import ResourceLocks, TaskContext
from .security import SecretStore
from .storage import Store
from .storage import now
from .maintenance import fingerprint, retention_candidates
from .profiles import sources_for


def build_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register("hermes_local", LocalHermesAdapter())
    registry.register("hermes_server", ServerHermesAdapter())
    registry.register("project", ProjectAdapter())
    registry.register("vault", VaultAdapter())
    registry.register("agent", AgentAdapter())
    return registry


class ApplicationService:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.registry = build_registry()
        self.locks = ResourceLocks()
        self.secrets = SecretStore(store.portable_root)

    def backup_root(self) -> Path:
        default = (self.store.portable_root / "backups") if self.store.portable_root else self.store.root / "backups"
        return Path(self.store.setting("backup_root", str(default))).expanduser()

    def lock_keys(self, resource: Resource, target: str = "") -> list[str]:
        keys = ["resource:" + resource.id]
        if resource.kind == "hermes_server":
            keys += ["server:" + str(resource.options.get("host", ""))]
        else:
            for name in ("path", "home", "backup_repo"):
                if resource.options.get(name):
                    keys.append("path:" + os.path.normcase(str(Path(resource.options[name]).expanduser().resolve())))
            if resource.kind == "agent":
                for component in sources_for(resource):
                    if component["path"]:
                        keys.append("path:" + os.path.normcase(str(Path(component["path"]).expanduser().resolve())))
            if target:
                keys.append("path:" + os.path.normcase(str(Path(target).expanduser().resolve())))
        return keys

    def observe(self, resource: Resource, context: TaskContext) -> dict:
        try:
            report = self.registry.get(resource).observe(resource, context)
        except UserError as exc:
            self.store.save_evidence("observe:" + resource.id, {"connected": False, "error": str(exc), "observed_at": now()})
            raise
        self.store.save_evidence("observe:" + resource.id, {**report, "observed_at": now()})
        return report

    def action(self, resource: Resource, action: str, context: TaskContext) -> dict:
        adapter = self.registry.get(resource)
        if action not in adapter.capabilities or not hasattr(adapter, action):
            raise UserError("此资源尚未支持该操作。")
        with self.locks.acquire(self.lock_keys(resource)):
            report = getattr(adapter, action)(resource, context)
            if action == "verify" and report.get("valid"):
                self.store.save_evidence("native:" + resource.id, {"created_at": report.get("created_at"), "verified_at": now()})
            return report

    def backup(self, resource: Resource, context: TaskContext, password: str = "") -> dict:
        if resource.kind in {"hermes_local", "hermes_server"}:
            result = self.action(resource, "backup", context)
            verification = self.action(resource, "verify", context)
            return {**result, "verified": verification.get("valid") is True}
        adapter = self.registry.get(resource)
        with self.locks.acquire(self.lock_keys(resource)):
            if isinstance(adapter, AgentAdapter) and adapter.is_running(resource):
                raise UserError("请停止本工具管理的 Agent 后再备份；外部进程需要自行退出。")
            result = archives.create_archive(resource, self.backup_root(), password, context)
            self.verify_backup(Path(result["archive"]), password, context)
            return {**result, "verified": True}

    def verify_backup(self, source: Path, password: str, context: TaskContext) -> dict:
        before = fingerprint(source)
        report = archives.verify_archive(source, password, context)
        if fingerprint(source) != before:
            raise UserError("备份文件在校验期间变化，请重试。")
        previous = self.store.evidence(str(source.resolve()))
        if previous.get("archive_sha256") != report["archive_sha256"]:
            previous = {}
        identity = {key: report[key] for key in ("resource_id", "kind", "engine", "created_at")}
        self.store.save_evidence(str(source.resolve()), {**previous, **before, **identity, "verified_at": now(), "archive_sha256": report["archive_sha256"]})
        return report

    def organize_agent_backups(self, repository: Path, context: TaskContext) -> dict:
        from .backup_repository import organize
        keys = ["path:" + os.path.normcase(str(path.resolve())) for path in (repository, self.backup_root())]
        with self.locks.acquire(keys):
            return organize(self.store, self.backup_root(), repository, context)

    def read_hermes_item(self, resource: Resource, category: str, identity: str, context: TaskContext) -> dict:
        from .hermes_library import read_item
        with self.locks.acquire(self.lock_keys(resource)):
            return read_item(resource, category, identity, context)

    def rehearse(self, resource: Resource, source: Path, password: str, context: TaskContext) -> dict:
        report = self.verify_backup(source, password, context)
        directory = self.store.root / "rehearsals"
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="trial-", dir=directory) as temporary:
            target = Path(temporary) / "restored"
            plan = self.plan_restore(resource, context, source=str(source), target=str(target), password=password)
            if plan.token != report["archive_sha256"]:
                raise UserError("备份已改变，请重试。")
            self.restore(resource, plan, context, password)
            count = 0
            with archives.open_archive(source, password, context, plan.token) as archive:
                manifest = archives.verify_open(archive, context)
                for record in manifest["files"]:
                    context.checkpoint()
                    relative = ".agent-manager-history.bundle" if record["path"] == "@git-bundle" else record["path"]
                    path = target / relative
                    if archives.path_token(path) != record["sha256"]:
                        raise UserError("演练恢复后的文件不一致。")
                    count += 1
        evidence = self.store.evidence(str(source.resolve()))
        self.store.save_evidence(str(source.resolve()), {**evidence, "rehearsed_at": now()})
        return {"valid": True, "identical_files": count, "note": "已在临时目录实际恢复并逐文件校验；临时资料已清理，原目录未覆盖。"}

    def automatic_backup(self, resource: Resource, context: TaskContext) -> dict:
        if resource.kind == "hermes_server" or not resource.options.get("automatic_backup"):
            raise UserError("此资源没有启用自动备份。")
        from .maintenance import is_due
        if not is_due(self.store, resource):
            return {"skipped": True, "note": "尚未到备份时间。"}
        password = ""
        if resource.kind in {"project", "vault", "agent"}:
            if resource.kind == "agent" and self.registry.get(resource).external_pids(resource):
                return {"skipped": True, "note": "原应用仍在运行，稍后自动重试。"}
            password = self.secrets.get(resource.id + ":backup") or ""
            if not password:
                return {"skipped": True, "note": "请先保存备份口令并解锁口令库。"}
        self.store.save_evidence("schedule:" + resource.id, {"last_attempt_at": now()})
        result = self.backup(resource, context, password)
        if resource.kind in {"project", "vault", "agent"}:
            keep = int(self.store.setting("backup_keep", 10))
            for old in retention_candidates(self.backup_root(), resource.id, keep, result["archive"]):
                context.checkpoint()
                try:
                    old_report = archives.verify_archive(old, password, context)
                    if old_report.get("resource_id") != resource.id:
                        context.log("旧版本归属不匹配，已保留。")
                        continue
                except UserError:
                    context.log("旧版本无法校验或口令已更换，已保留。")
                    continue
                old.unlink()
                old.with_suffix(".json").unlink(missing_ok=True)
        return result

    def plan_restore(self, resource: Resource, context: TaskContext, *, source: str = "", target: str = "", password: str = "", folders: dict[str, str] | None = None) -> RestorePlan:
        with self.locks.acquire(self.lock_keys(resource, target)):
            if resource.kind == "hermes_local":
                return self.registry.get(resource).plan_restore(resource, context)
            if resource.kind == "hermes_server":
                return self.registry.get(resource).plan_restore(resource, context, target)
            return archives.plan_restore(resource, Path(source), Path(target), password, context, folders)

    def restore(self, resource: Resource, plan: RestorePlan, context: TaskContext, password: str = "") -> dict:
        with self.locks.acquire(self.lock_keys(resource, plan.target)):
            if resource.kind in {"hermes_local", "hermes_server"}:
                return self.registry.get(resource).restore(resource, plan, context)
            return archives.apply_restore(plan, password, context)

    def diagnostics(self) -> dict:
        return {"platform": sys.platform, "python": sys.version.split()[0], "data_directory": str(self.store.root),
                "tools": {name: shutil.which(name) or "未找到；可在资源设置中指定路径" for name in ("git", "ssh", "gpg", "python", "python3")},
                "adapters": self.registry.describe(), "note": "Linux/macOS 系统凭据存储需要已解锁的钥匙串；Hermes 操作需要其备份工具的 Python 依赖。"}

    def running_owned_ids(self) -> list[str]:
        identities = []
        for kind in ("agent", "hermes_local"):
            identities.extend(self.registry.for_kind(kind).running_ids())
        return identities
