from __future__ import annotations

import shutil
import os
import sys
from pathlib import Path

from . import archives
from .adapters.agents import AgentAdapter
from .adapters.local import LocalHermesAdapter
from .adapters.projects import ProjectAdapter
from .adapters.server import ServerHermesAdapter
from .domain import AdapterRegistry, Resource, RestorePlan, UserError
from .runtime import ResourceLocks, TaskContext
from .security import SecretStore
from .storage import Store


def build_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register("hermes_local", LocalHermesAdapter())
    registry.register("hermes_server", ServerHermesAdapter())
    registry.register("project", ProjectAdapter())
    registry.register("vault", ProjectAdapter())
    registry.register("agent", AgentAdapter())
    return registry


class ApplicationService:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.registry = build_registry()
        self.locks = ResourceLocks()
        self.secrets = SecretStore()

    def backup_root(self) -> Path:
        return Path(self.store.setting("backup_root", str(self.store.root / "backups"))).expanduser()

    def lock_keys(self, resource: Resource, target: str = "") -> list[str]:
        keys = ["resource:" + resource.id]
        if resource.kind == "hermes_server":
            keys += ["server:" + str(resource.options.get("host", ""))]
        else:
            for name in ("path", "home", "backup_repo"):
                if resource.options.get(name):
                    keys.append("path:" + os.path.normcase(str(Path(resource.options[name]).expanduser().resolve())))
            if target:
                keys.append("path:" + os.path.normcase(str(Path(target).expanduser().resolve())))
        return keys

    def observe(self, resource: Resource, context: TaskContext) -> dict:
        return self.registry.get(resource).observe(resource, context)

    def action(self, resource: Resource, action: str, context: TaskContext) -> dict:
        adapter = self.registry.get(resource)
        if action not in adapter.capabilities or not hasattr(adapter, action):
            raise UserError("此资源尚未支持该操作。")
        with self.locks.acquire(self.lock_keys(resource)):
            return getattr(adapter, action)(resource, context)

    def backup(self, resource: Resource, context: TaskContext, password: str = "") -> dict:
        if resource.kind in {"hermes_local", "hermes_server"}:
            return self.action(resource, "backup", context)
        adapter = self.registry.get(resource)
        with self.locks.acquire(self.lock_keys(resource)):
            if isinstance(adapter, AgentAdapter) and adapter.is_running(resource):
                raise UserError("请停止本工具管理的 Agent 后再备份；外部进程需要自行退出。")
            return archives.create_archive(resource, self.backup_root(), password, context)

    def plan_restore(self, resource: Resource, context: TaskContext, *, source: str = "", target: str = "", password: str = "") -> RestorePlan:
        with self.locks.acquire(self.lock_keys(resource, target)):
            if resource.kind == "hermes_local":
                return self.registry.get(resource).plan_restore(resource, context)
            if resource.kind == "hermes_server":
                return self.registry.get(resource).plan_restore(resource, context, target)
            return archives.plan_restore(resource, Path(source), Path(target), password, context)

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
