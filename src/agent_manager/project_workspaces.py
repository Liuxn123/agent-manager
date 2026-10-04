"""Project lifecycle compatible with the user's projects.json v2 convention."""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

from .domain import UserError
from .runtime import TaskContext

STATES = {"candidate": "待开展", "active": "进行中", "paused": "暂停", "completed": "已完成", "archived": "已归档"}
MANAGEMENT_FILES = ("README.md", "AGENTS.md", "agent/README.md", "agent/STATUS.md", "agent/TASKS.md", "agent/HANDOFF.md")


def today() -> str:
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def linked(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except FileNotFoundError:
        return False


def field(text: str, name: str) -> str:
    front = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    values = re.findall(r"^" + re.escape(name) + r":[ \t]*(.*)$", front.group(1), re.M) if front else []
    if len(values) != 1:
        raise UserError(f"管理文档缺少或重复字段：{name}")
    return values[0].strip().strip('\"\'')


def replace_state(text: str, state: str) -> str:
    field(text, "status")
    front, body = text.split("---", 2)[1:]
    front = re.sub(r"(?m)^status:.*$", "status: " + state, front)
    if re.search(r"(?m)^updated:", front):
        front = re.sub(r"(?m)^updated:.*$", "updated: " + today(), front)
    else:
        front += "updated: " + today() + "\n"
    body = re.sub(r"(?m)^- 生命周期：.*$", "- 生命周期：" + state, body)
    return "---" + front + "---" + body


class ProjectWorkspace:
    def __init__(self, root: Path):
        selected = Path(os.path.abspath(root.expanduser()))
        if linked(selected):
            raise UserError("请选择实际工作区根目录，不能使用目录联接入口。")
        self.root = selected.resolve()
        self.path("")
        self.registry_path = self.path("agent/projects.json")

    def path(self, relative: str, allow_link: bool = False) -> Path:
        part = Path(relative)
        if part.anchor or ".." in part.parts or re.search(r'[<>:"|?*\x00-\x1f]', relative):
            raise UserError("工作区路径越界。")
        result = self.root / part
        for parent in reversed(self.root.parents):
            if linked(parent):
                raise UserError("工作区父目录不能是联接或符号链接。")
        chain = [self.root]
        for component in part.parts:
            chain.append(chain[-1] / component)
        for candidate in chain:
            if linked(candidate) and not (allow_link and candidate == result):
                raise UserError("管理路径不能穿过联接或符号链接：" + str(candidate))
        return result

    @staticmethod
    def read(path: Path) -> str:
        if path.stat().st_size > 2_000_000:
            raise UserError("管理文件过大，请先检查内容。")
        return path.read_text(encoding="utf-8-sig")

    def registry(self) -> dict:
        try:
            data = json.loads(self.read(self.registry_path))
        except (OSError, ValueError) as exc:
            raise UserError("未找到有效的 agent/projects.json；请选择工作区或初始化新的空工作区。") from exc
        if data.get("schema_version") != 2 or not isinstance(data.get("projects"), list) or not isinstance(data.get("issued_ids"), list):
            raise UserError("需要 projects.json v2 登记表。")
        ids, paths, entries = set(), set(), set()
        issued = data["issued_ids"]
        if len(set(issued)) != len(issued) or any(not re.fullmatch(r"P-\d{4}-\d{3}", i) or i.endswith("000") for i in issued):
            raise UserError("已发编号清单有重复或无效编号。")
        for item in data["projects"]:
            identity, name, relative = item.get("id", ""), item.get("name", ""), item.get("path", "")
            if not name or any(c in name for c in "\r\n|") or not re.fullmatch(r"(?:projects|archive)/[^/\\]+", relative):
                raise UserError("项目名称或登记路径无效。")
            if item.get("legacy") is True:
                if identity != "L-" + name or not relative.startswith("archive/"):
                    raise UserError("旧归档登记不一致。")
            elif item.get("legacy") is not False or identity not in issued or Path(relative).name != identity + "-" + name:
                raise UserError("项目编号、名称与目录不一致。")
            entry = item.get("vault_entry")
            if item.get("link_policy") not in {"junction", "symlink", "external", "plain-text"}:
                raise UserError("项目入口策略无效。")
            if entry and not re.fullmatch(r"myself/03-项目/[^/\\]+", entry):
                raise UserError("Obsidian 入口无效。")
            if identity in ids or relative in paths or entry and entry in entries:
                raise UserError("项目登记重复。")
            ids.add(identity); paths.add(relative)
            if entry:
                entries.add(entry)
                self.path(entry, allow_link=True)
            self.path(relative)
        return data

    @contextmanager
    def lock(self, action: str):
        lock = self.path("agent/.manage.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            stream = lock.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise UserError("工作区已有管理锁。请等待其他工具完成；残留锁需要核对进程后处理。") from exc
        with stream:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(), "action": action, "root": str(self.root), "started_at": datetime.now(timezone.utc).isoformat()}, stream)
            stream.flush(); os.fsync(stream.fileno())
        try:
            yield
        finally:
            lock.unlink()

    def write(self, path: Path, text: str, expected: str | None = None):
        relative = path.relative_to(self.root).as_posix()
        self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if expected is None:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            return
        if self.read(path) != expected:
            raise UserError("管理文件已被其他程序修改，停止覆盖：" + str(path))
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        if self.read(path) != expected:
            temporary.unlink()
            raise UserError("管理文件在写入前发生变化，停止覆盖。")
        os.replace(temporary, path)

    def save_registry(self, data: dict, expected: str):
        data["updated"] = today()
        self.write(self.registry_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", expected)

    def initialize(self, context: TaskContext) -> dict:
        if self.root.exists() and any(self.root.iterdir()):
            raise UserError("初始化工作区需要一个新目录或空目录，不会重建已有工作区。")
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock("Initialize"):
            for area in ("projects", "archive", "myself/03-项目"):
                self.path(area).mkdir(parents=True, exist_ok=True)
            self.write(self.registry_path, json.dumps({"schema_version": 2, "updated": today(), "projects": [], "issued_ids": []}, indent=2) + "\n")
            self.write(self.path("AGENTS.md"), "# 工作区规则\n\n编号不复用；身份与路径以 agent/projects.json 为准。\n进度以项目 STATUS 为准，任务只维护 TASKS 指定的真源，日志追加到 HANDOFF。\n项目之间默认隔离，不读凭据；创建与移动先检查冲突。\n同步副本不能同时分配编号；同一时刻由一台电脑管理本工作区。\n")
            self.update_indexes(self.registry())
        context.log("已初始化工作区管理结构；未创建 Git 仓库或定时任务。")
        return {"workspace": str(self.root), "initialized": True}

    def list_projects(self, context: TaskContext) -> dict:
        rows = []
        for project in self.registry()["projects"]:
            context.checkpoint()
            path = self.path(project["path"])
            try:
                status = self.read(self.path(project["path"] + "/agent/STATUS.md"))
                state = field(status, "status")
                if state not in STATES or field(status, "project_id") != project["id"]:
                    raise UserError("状态与项目编号不一致。")
                error = ""
            except (OSError, UserError) as exc:
                state, error = "unknown", str(exc)
            rows.append({**project, "directory": str(path), "state": state, "error": error})
        return {"projects": rows, "workspace": str(self.root)}

    def document(self, identity: str, relative: str) -> dict:
        if relative not in MANAGEMENT_FILES:
            raise UserError("只允许打开项目管理文档。")
        item = self.project(identity)
        text = self.read(self.path(item["path"] + "/" + relative))
        return {"text": text, "path": str(self.path(item["path"] + "/" + relative))}

    def project(self, identity: str) -> dict:
        item = next((p for p in self.registry()["projects"] if p["id"] == identity), None)
        if item is None:
            raise UserError("项目登记已变化，请刷新列表。")
        return item

    def _link(self, entry: str, target: Path):
        path = self.path(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or linked(path):
            raise UserError("Obsidian 入口已经存在，停止创建。")
        if os.name == "nt":
            # Arguments travel as data; no interpolated shell command or cmd deletion.
            script = "New-Item -ItemType Junction -Path $env:AM_PROJECT_LINK -Target $env:AM_PROJECT_TARGET -ErrorAction Stop | Out-Null"
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], env={**os.environ, "AM_PROJECT_LINK": str(path), "AM_PROJECT_TARGET": str(target)}, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise UserError("未能建立 Obsidian 联接；请检查工作区和入口权限，保留创建现场。")
        else:
            path.symlink_to(target, target_is_directory=True)

    def create(self, name: str, goal: str, use_entry: bool, context: TaskContext) -> dict:
        if not name.strip() or len(name) > 80 or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((".", " ")) or name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1,10)), *("LPT" + str(i) for i in range(1,10))}:
            raise UserError("请使用有效项目名称，不包含路径或文件名保留字符。")
        if not goal.strip() or len(goal) > 2000:
            raise UserError("请填写项目目标（不超过 2000 个字符）。")
        with self.lock("New"):
            observed = self.read(self.registry_path)
            data = self.registry()
            self.index_outputs(data)  # Validate managed sections before any creation.
            if any(p["name"].casefold() == name.casefold() for p in data["projects"]):
                raise UserError("已有同名项目，请继续使用已有项目。")
            known = {p["path"] for p in data["projects"]}
            for area in ("projects", "archive"):
                for child in self.path(area).iterdir():
                    if child.is_dir() and child.relative_to(self.root).as_posix() not in known:
                        raise UserError("发现未登记项目目录，请先核对登记表，不自动重复初始化。")
            year = today()[:4]
            identifiers = list(data["issued_ids"])
            for area in ("projects", "archive", "myself/03-项目"):
                identifiers.extend(p.name for p in self.path(area).iterdir())
            numbers = [int(m.group(1)) for value in identifiers if (m := re.match("P-" + year + r"-(\d{3})(?:-|$)", value))]
            number = max(numbers, default=0) + 1
            if number > 999:
                raise UserError("本年度项目编号已满。")
            identity = f"P-{year}-{number:03d}"
            folder = identity + "-" + name
            relative, entry = "projects/" + folder, "myself/03-项目/" + folder if use_entry else None
            for candidate in (relative, "archive/" + folder, "myself/03-项目/" + folder):
                if self.path(candidate).exists():
                    raise UserError("项目或入口目标已存在。")
            data["issued_ids"].append(identity)
            self.save_registry(data, observed)  # Reserve forever, even if following creation fails.
            observed = self.read(self.registry_path)
            target = self.path(relative)
            target.mkdir()
            for filename in ("README.md", "STATUS.md", "TASKS.md", "HANDOFF.md", "AGENTS.md"):
                existing = self.path("agent/templates/" + filename)
                text = self.read(existing) if existing.is_file() else default_template(filename)
                for key, value in {"NAME": name, "DATE": today(), "ID": identity, "STATE": "candidate", "GOAL": goal, "PATH": str(target), "VAULT": str(self.root / entry) if entry else "无（只保留索引）", "WORKSPACE": str(self.root)}.items():
                    text = text.replace("{{" + key + "}}", value)
                if not entry and filename in {"AGENTS.md", "HANDOFF.md"}:
                    text += "\n本次入口策略：仅索引；未建立 Obsidian 联接。\n"
                self.write(target / (filename if filename == "AGENTS.md" else "agent/" + filename), text)
            self.write(target / "README.md", f"# {name}\n\n编号：{identity}\n\n[接续入口](agent/README.md) · [状态](agent/STATUS.md) · [任务](agent/TASKS.md) · [日志与交接](agent/HANDOFF.md)\n")
            if entry:
                self._link(entry, target)
            data["projects"].append({"id": identity, "name": name, "path": relative, "vault_entry": entry, "legacy": False, "link_policy": ("junction" if os.name == "nt" else "symlink") if entry else "plain-text"})
            self.save_registry(data, observed)
            self.update_indexes(data)
        context.log("已创建必要管理文件并更新索引；业务任务和备份尚未验证。")
        return {"project_id": identity, "directory": str(target), "state": "candidate"}

    def index_outputs(self, data: dict) -> list[tuple[Path, str, str | None]]:
        rows = {"projects": [], "archive": []}
        for item in data["projects"]:
            status = self.read(self.path(item["path"] + "/agent/STATUS.md"))
            state = field(status, "status")
            if state not in STATES or field(status, "project_id") != item["id"]:
                raise UserError("项目状态字段不合规，停止更新索引。")
            entry = "[[" + item["vault_entry"][len("myself/"):] + "/README]]" if item.get("vault_entry") else "无"
            context_link = "纯文本路径；不建 Vault 链接" if item["link_policy"] == "plain-text" else "[Agent 接续](" + (self.root / item["path"] / "agent/README.md").as_uri() + ")"
            rows[item["path"].split("/")[0]].append(f"| {item['name']} | {item['id']} | {state} | {entry} | `{self.root / item['path']}` | {context_link} |")
        outputs = []
        for area, title in (("projects", "项目总览"), ("archive", "归档项目索引")):
            path = self.path("myself/03-项目/" + title + ".md")
            block = "<!-- agent-index:start -->\n<!-- 由 Manage-Projects.ps1 -Action Index 生成；身份取 projects.json，状态取 STATUS.md -->\n| 项目 | 管理编号 | 生命周期 | Vault 入口 | 完整资料真源 | 接续 |\n|---|---|---|---|---|---|\n" + "\n".join(rows[area]) + "\n<!-- agent-index:end -->"
            before = self.read(path) if path.exists() else None
            if before is not None:
                pattern = r"<!-- agent-index:start -->.*?<!-- agent-index:end -->"
                if len(re.findall(pattern, before, re.S)) != 1:
                    raise UserError("索引管理区块缺失或重复，不覆盖人工内容。")
                after = re.sub(pattern, lambda _: block, before, flags=re.S)
            else:
                after = f"---\ntitle: {title}\ncreated: {today()}\nstatus: active\ntype: project-index\ntags: [project, index]\n---\n\n# {title}\n\n{block}\n"
            outputs.append((path, after, before))
        architecture = self.path("myself/99-系统/搭建计划.md")
        if architecture.exists():
            before = self.read(architecture)
            pattern = r"<!-- agent-architecture:start -->.*?<!-- agent-architecture:end -->"
            if len(re.findall(pattern, before, re.S)) != 1:
                raise UserError("搭建计划缺少唯一自动路径区块，不覆盖人工内容。")
            block = "<!-- agent-architecture:start -->\n<!-- 由登记表生成当前路径映射，不在此手工编辑 -->\n| 编号 | 完整资料路径（工作区相对路径） | Vault 入口 | 策略 |\n|---|---|---|---|\n" + "\n".join(f"| {p['id']} | `{p['path']}` | `{p.get('vault_entry') or '无'}` | {p['link_policy']} |" for p in data["projects"]) + "\n<!-- agent-architecture:end -->"
            outputs.append((architecture, re.sub(pattern, lambda _: block, before, flags=re.S), before))
        return outputs

    def update_indexes(self, data: dict):
        for path, after, before in self.index_outputs(data):
            self.write(path, after, before)

    def append_log(self, identity: str, note: str, context: TaskContext) -> dict:
        if not note.strip() or len(note) > 20_000:
            raise UserError("请填写日志内容（不超过 20000 个字符）。")
        with self.lock("Log"):
            item = self.project(identity)
            if item["path"].startswith("archive/"):
                raise UserError("归档项目先重新启用；阅读历史不会修改归档。")
            path = self.path(item["path"] + "/agent/HANDOFF.md")
            before = self.read(path)
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            self.write(path, before.rstrip() + f"\n\n## {stamp} — 用户日志\n\n{note.strip()}\n", before)
        return {"project_id": identity, "saved": True, "path": str(path)}

    def save_document(self, identity: str, relative: str, text: str, original: str, context: TaskContext) -> dict:
        if relative not in {"agent/STATUS.md", "agent/TASKS.md"} or len(text) > 100_000:
            raise UserError("只允许编辑当前状态和任务入口，内容不超过 100000 字符。")
        with self.lock("Edit"):
            item = self.project(identity)
            if item["path"].startswith("archive/"):
                raise UserError("请先重新启用归档项目。")
            if field(text, "project_id") != identity:
                raise UserError("请保留文档中的 project_id。")
            if relative == "agent/STATUS.md" and field(text, "status") not in set(STATES) - {"archived"}:
                raise UserError("归档请使用归档按钮；其他状态为 candidate/active/paused/completed。")
            self.index_outputs(self.registry())
            self.write(self.path(item["path"] + "/" + relative), text, original)
            self.update_indexes(self.registry())
        return {"project_id": identity, "saved": True}

    def manifest(self, source: Path, context: TaskContext) -> dict:
        records = {}
        for directory, subdirs, files in os.walk(source, followlinks=False):
            context.checkpoint()
            for name in subdirs + files:
                path = Path(directory) / name
                if linked(path):
                    raise UserError("项目含联接/符号链接，请先核对移动后的引用：" + str(path.relative_to(source)))
            records[Path(directory).relative_to(source).as_posix() + "/"] = "directory"
            for name in files:
                path = Path(directory) / name
                if not path.is_file():
                    raise UserError("项目含非常规文件，不能自动归档。")
                digest = hashlib.sha256()
                before = path.stat()
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        context.checkpoint(); digest.update(chunk)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise UserError("项目仍在写入，请停止相关任务后重试。")
                records[path.relative_to(source).as_posix()] = digest.hexdigest()
        return records

    def plan_move(self, identity: str, resume: bool, context: TaskContext) -> dict:
        item = self.project(identity)
        if item["legacy"]:
            raise UserError("旧 L-归档应作为新目标另建编号项目；原归档保持原位。")
        expected_area = "archive" if resume else "projects"
        if not item["path"].startswith(expected_area + "/"):
            raise UserError("项目当前位置不符合此次操作。")
        source = self.path(item["path"])
        target = self.path(("projects" if resume else "archive") + "/" + source.name)
        if not source.is_dir() or target.exists():
            raise UserError("来源不存在或目标已存在，停止移动。")
        if source.stat().st_dev != target.parent.stat().st_dev:
            raise UserError("归档和项目目录需位于同一文件系统，避免跨盘部分移动。")
        for name in MANAGEMENT_FILES:
            self.read(self.path(item["path"] + "/" + name))
        status = self.read(self.path(item["path"] + "/agent/STATUS.md"))
        if field(status, "project_id") != identity or field(status, "status") not in STATES:
            raise UserError("项目状态文件不一致。")
        entry = item.get("vault_entry")
        if entry:
            link = self.path(entry, allow_link=True)
            if not linked(link) or link.resolve() != source.resolve():
                raise UserError("Obsidian 入口不是指向当前项目的已登记联接，停止移动。")
        self.index_outputs(self.registry())
        records = self.manifest(source, context)
        payload = {"registry": self.read(self.registry_path), "records": records, "item": item, "resume": resume}
        token = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {"project_id": identity, "source": str(source), "target": str(target), "resume": resume, "token": token, "file_count": sum(v != "directory" for v in records.values()), "backup": "备份/验收证据由用户核对，本操作不宣称已验证", "entry": entry or "无", "records": records}

    def move(self, plan: dict, reason: str, context: TaskContext) -> dict:
        if not reason.strip():
            raise UserError("请填写归档原因或重新启用后的下一步。")
        with self.lock("Resume" if plan["resume"] else "Archive"):
            current = self.plan_move(plan["project_id"], plan["resume"], context)
            if current["token"] != plan["token"] or current["target"] != plan["target"]:
                raise UserError("项目或登记表在预览后变化，请重新预检。")
            context.checkpoint()
            source, target = Path(current["source"]), Path(current["target"])
            data = self.registry(); observed = self.read(self.registry_path)
            item = next(p for p in data["projects"] if p["id"] == plan["project_id"])
            state = "active" if plan["resume"] else "archived"
            journal = self.path("agent/history/" + uuid4().hex + "-move.json")
            self.write(journal, json.dumps({**current, "reason": reason, "state": "prepared"}, ensure_ascii=False, indent=2))
            try:
                os.rename(source, target)
                # Do not interrupt critical directory/registry/link switching.
                if self.manifest(target, TaskContext()) != current["records"]:
                    raise UserError("移动后文件清单不同，保留现场并停止。")
                if item.get("vault_entry"):
                    entry = self.path(item["vault_entry"], allow_link=True)
                    if not linked(entry) or entry.resolve() != source.resolve():
                        raise UserError("入口在移动期间变化，请按移动记录修复。")
                    if os.name == "nt":
                        os.rmdir(entry)  # Remove only the verified junction, never recurse.
                    else:
                        entry.unlink()
                    self._link(item["vault_entry"], target)
                item["path"] = target.relative_to(self.root).as_posix()
                for name in ("README.md", "AGENTS.md", "agent/STATUS.md"):
                    path = self.path(item["path"] + "/" + name)
                    before = self.read(path)
                    after = before.replace(str(source), str(target)).replace(source.as_posix(), target.as_posix())
                    if name == "agent/STATUS.md":
                        after = replace_state(after, state)
                        after += f"\n\n## {today()} — {'重新启用' if plan['resume'] else '归档'}\n\n{reason.strip()}\n备份验证：以已有证据为准，本次未执行备份验证。\n"
                    self.write(path, after, before)
                handoff = self.path(item["path"] + "/agent/HANDOFF.md")
                before = self.read(handoff)
                self.write(handoff, before.rstrip() + f"\n\n## {today()} — 管家{'重新启用' if plan['resume'] else '归档'}\n- 原位置：`{source}`\n- 新位置：`{target}`\n- 原因/下一步：{reason.strip()}\n- 验证：移动后 {current['file_count']} 个文件哈希及目录清单一致；未执行备份恢复验证。\n- 原代码绝对路径和 Git/同步设置需核对，未自动改写业务文件。\n", before)
                self.save_registry(data, observed)
                self.update_indexes(data)
                before = self.read(journal)
                self.write(journal, json.dumps({**current, "reason": reason, "state": "completed"}, ensure_ascii=False, indent=2), before)
            except (OSError, UserError) as exc:
                raise UserError("移动过程未全部完成，禁止盲目重试。请保留目录并按记录核对：" + str(journal)) from exc
        return {"project_id": item["id"], "directory": str(target), "state": state, "verified_files": current["file_count"]}


def default_template(filename: str) -> str:
    if filename == "AGENTS.md":
        return "# {{ID}} — {{NAME}} 项目规则\n\n- 当前完整目录：`{{PATH}}`。\n- Obsidian 入口：`{{VAULT}}`。\n- 先读 agent/README → STATUS → TASKS → HANDOFF 最近两条。\n- 项目之间隔离；保留原文与用户修改，不写凭据，不自动提交发布。\n- 状态以 STATUS 为准，任务以 TASKS 为准，日志追加 HANDOFF。\n"
    kinds = {"README.md": "project-agent-index", "STATUS.md": "project-status", "TASKS.md": "project-tasks", "HANDOFF.md": "project-handoff"}
    front = "---\ntitle: \"{{NAME}} " + filename + "\"\ncreated: {{DATE}}\nupdated: {{DATE}}\nstatus: " + ("{{STATE}}" if filename == "STATUS.md" else "active") + "\ntype: " + kinds[filename] + "\nproject_id: {{ID}}\ntags: [project, agent]\n---\n\n"
    contents = {
        "README.md": "# {{NAME}} 接续入口\n\n先读 [项目规则](../AGENTS.md) → [当前状态](STATUS.md) → [任务](TASKS.md) → [日志与交接](HANDOFF.md)。\n只维护一份状态和任务真源；业务目录按需建立。\n",
        "STATUS.md": "# {{NAME}} 当前状态\n\n## 目标与验收\n- 目标：{{GOAL}}\n- 验收条件：待补。\n\n## 当前进度\n- 生命周期：{{STATE}}\n- 当前阶段：待明确。\n- 下一步：明确验收条件并选择第一项任务。\n- 阻塞：待补。\n- 任务入口：[TASKS.md](TASKS.md)。\n\n## 备份与恢复\n- 备份与恢复验证：未验证。\n",
        "TASKS.md": "# {{NAME}} 任务清单\n\n本文件是唯一任务真源；已有任务清单时改为指向它，避免重复维护。\n\n| 编号 | 动作 | 验收条件 | 状态 | 证据 |\n|---|---|---|---|---|\n| T-001 | 明确验收条件 | STATUS 写清可验证完成条件 | todo | 待补 |\n",
        "HANDOFF.md": "# {{NAME}} 日志与交接\n\n按时间追加，保留历史。记录实际改动、验证、未完成和下一步。\n\n## {{DATE}} — 初始化\n- 目标：{{GOAL}}\n- 已做：建立管理结构。业务、备份与运行环境未验证。\n- 下一步：明确验收条件。\n",
    }
    return front + contents[filename]
