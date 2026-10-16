"""One-time, explicit migration to selfManger; never imported by the application."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
from contextlib import closing


OLD_SERVICE = "personal-agent-manager"
NEW_SERVICE = "selfManger"


def fingerprint(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ordinary_tree(root):
    records = {}
    for directory, subdirs, files in os.walk(root, followlinks=False):
        for name in ["", *subdirs, *files]:
            path = Path(directory) / name if name else Path(directory)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise RuntimeError("Migration source contains a link; inspect it before proceeding")
        for name in files:
            path = Path(directory) / name
            if not path.is_file():
                raise RuntimeError("Migration source contains a non-regular file")
            records[path.relative_to(root).as_posix()] = fingerprint(path)
    return records


def counts(db):
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {name: db.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}


def migrate_directory(source, destination, snapshot, *, apply=False):
    source, destination, snapshot = map(lambda p: Path(p).absolute(), (source, destination, snapshot))
    if source.parent != destination.parent or destination.exists() or snapshot.exists() or snapshot.resolve().is_relative_to(source.resolve()):
        raise RuntimeError("Directory migration requires a new sibling target and separate snapshot")
    records = ordinary_tree(source)
    if not apply:
        return {"files": len(records), "apply": False}
    shutil.copytree(source, snapshot)
    if ordinary_tree(snapshot) != records or ordinary_tree(source) != records:
        raise RuntimeError("Directory snapshot verification failed")
    source.rename(destination)
    if ordinary_tree(destination) != records:
        destination.rename(source)
        raise RuntimeError("Directory content changed during migration")
    return {"files": len(records), "snapshot_verified": True, "migrated": True}


def migrate_workbench_references(database, source, destination, snapshot, knowledge_root, *, apply=False):
    """Follow the explicit notes move, including import identities to prevent duplicates."""
    database, source, destination, snapshot, knowledge_root = map(lambda p: Path(p).absolute(),
        (database, source, destination, snapshot, knowledge_root))
    if source.exists() or not destination.is_dir() or snapshot.exists() or not database.is_file():
        raise RuntimeError("Move notes first; use an existing database and a new snapshot file")
    old = source.relative_to(knowledge_root).as_posix()
    new = destination.relative_to(knowledge_root).as_posix()
    root_id = hashlib.sha256(str(knowledge_root.resolve()).casefold().encode()).hexdigest()
    old_ref = "legacy:daily:" + str(source) + ":"
    new_ref = "legacy:daily:" + str(destination) + ":"
    with closing(sqlite3.connect(database)) as db:
        before = counts(db)
        refs = [(identity, value, new_ref + value[len(old_ref):]) for identity, value in
                db.execute("SELECT id,source_ref FROM planned_tasks WHERE source_ref IS NOT NULL") if value.startswith(old_ref)]
        files = [(table, rowid, relative, new + relative[len(old):])
                 for table in ("knowledge_files", "knowledge_metadata")
                 for rowid, relative in db.execute("SELECT rowid,relative_path FROM " + table + " WHERE root_id=?", (root_id,))
                 if relative == old or relative.startswith(old + "/")]
        result = {"task_references": len(refs), "knowledge_references": len(files)}
        if not apply:
            return result
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(snapshot)) as copy:
            db.backup(copy)
            if counts(copy) != before or copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Reference snapshot verification failed")
        with db:
            row = db.execute("SELECT value FROM settings WHERE key='legacy_task_import_v1'").fetchone()
            if row:
                marker = json.loads(row[0])
                updated = transform(marker, source, destination)
                db.execute("UPDATE settings SET value=? WHERE key='legacy_task_import_v1'", (json.dumps(updated, ensure_ascii=False),))
            for identity, previous, updated in refs:
                db.execute("UPDATE planned_tasks SET source_ref=? WHERE id=?", (updated, identity))
            for table, rowid, previous, updated in files:
                db.execute("UPDATE " + table + " SET relative_path=? WHERE rowid=?", (updated, rowid))
                if table == "knowledge_files":
                    folder = str(Path(updated).parent).replace("\\", "/")
                    db.execute("UPDATE knowledge_files SET folder=? WHERE rowid=?", (folder, rowid))
            if counts(db) != before or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Reference migration verification failed")
        return {**result, "snapshot_verified": True}


def transform(value, old_root, new_root):
    if isinstance(value, dict):
        return {key: transform(item, old_root, new_root) for key, item in value.items()}
    if isinstance(value, list):
        return [transform(item, old_root, new_root) for item in value]
    if isinstance(value, str):
        # Only application-owned path prefixes; do not replace user prose/logs.
        for old, new in ((str(old_root), str(new_root)),
                         (str(Path(__file__).resolve().parents[2] / "agent-manager"), str(Path(__file__).resolve().parents[1]))):
            if value == old or value.startswith(old + os.sep):
                return new + value[len(old):]
        if value.startswith(".xiaonian/"):
            return ".selfManger/" + value[len(".xiaonian/"):]
    return value


def project_plan(workspace):
    if workspace is None:
        return [], []
    workspace = Path(workspace).resolve()
    registry = json.loads((workspace / "agent/projects.json").read_text(encoding="utf-8-sig"))
    renames, edits = [], []
    for item in registry["projects"]:
        project = workspace / item["path"]
        if not project.resolve().is_relative_to(workspace):
            raise RuntimeError("Registered project escapes workspace")
        old = project / ".xiaonian"
        new = project / ".selfManger"
        if old.exists():
            if new.exists():
                raise RuntimeError("Project already has both old and new configuration directories")
            ordinary_tree(old)
            renames.append((old, new))
        for relative in ("AGENTS.md", "agent/README.md", "agent/PROMPTS.md", "agent/TASKS-AI.md", "agent/TASKS-MANUAL.md", ".xiaonian/config.json", ".xiaonian/RESOURCES.md"):
            path = project / relative
            if not path.is_file():
                continue
            if path.is_symlink() or path.stat().st_size > 2_000_000:
                raise RuntimeError("Generated management file requires manual inspection")
            raw = path.read_bytes()
            before = raw.decode("utf-8-sig")
            after = before.replace("<!-- agent-manager:tasks:", "<!-- selfManger:tasks:")
            after = after.replace(".xiaonian/", ".selfManger/").replace("小年助手项目配置", "selfManger项目配置")
            if after != before:
                edits.append((path, raw, after.encode("utf-8")))
    return renames, edits


def migrate(source, destination, snapshot, *, workspace=None, keyring=None, apply=False):
    source, destination, snapshot = map(lambda p: Path(p).absolute(), (source, destination, snapshot))
    if source == destination or destination.exists() or snapshot.exists():
        raise RuntimeError("Use distinct existing source and new destination/snapshot paths")
    if source.parent != destination.parent or snapshot.resolve().is_relative_to(source.resolve()):
        raise RuntimeError("Data rename must stay in the same parent; snapshot must be outside source")
    original = ordinary_tree(source)
    db_path = source / "manager.sqlite3"
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Source database integrity check failed")
        before_counts = counts(db)
        resource_ids = [row[0] for row in db.execute("SELECT id FROM resources")]
    renames, edits = project_plan(workspace)
    result = {"counts": before_counts, "project_directories": len(renames), "management_files": len(edits), "credential_count": 0}
    if not apply:
        return result
    snapshot.mkdir(parents=True)
    shutil.copytree(source, snapshot / "application-data")
    if ordinary_tree(snapshot / "application-data") != original or ordinary_tree(source) != original:
        raise RuntimeError("Snapshot differs from source; no migration performed")
    for i, (old, new) in enumerate(renames):
        shutil.copytree(old, snapshot / "project-configs" / str(i))
        if ordinary_tree(old) != ordinary_tree(snapshot / "project-configs" / str(i)):
            raise RuntimeError("Project snapshot verification failed")
    for i, (path, raw, after) in enumerate(edits):
        (snapshot / "management-files").mkdir(exist_ok=True)
        (snapshot / "management-files" / str(i)).write_bytes(raw)
    credentials = {}
    if keyring is not None:
        identities = {"common:backup", "git:https-token", *(identity + ":backup" for identity in resource_ids)}
        current = keyring.get_credential(OLD_SERVICE, None)
        if current:
            identities.add(current.username)
        for identity in identities:
            value = keyring.get_password(OLD_SERVICE, identity)
            if value is None:
                continue
            existing = keyring.get_password(NEW_SERVICE, identity)
            if existing is not None and existing != value:
                raise RuntimeError("Destination credential conflicts; no overwrite")
            credentials[identity] = value
        for identity, value in credentials.items():
            keyring.set_password(NEW_SERVICE, identity, value)
            if keyring.get_password(NEW_SERVICE, identity) != value:
                raise RuntimeError("New credential storage verification failed")
    changed = []
    moved = []
    source.rename(destination)
    try:
        with closing(sqlite3.connect(destination / "manager.sqlite3")) as db, db:
            for table, column in (("settings", "value"), ("resources", "document")):
                for rowid, text in db.execute(f'SELECT rowid, "{column}" FROM "{table}"').fetchall():
                    value = json.loads(text)
                    updated = transform(value, source, destination)
                    if updated != value:
                        db.execute(f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?', (json.dumps(updated, ensure_ascii=False), rowid))
            if counts(db) != before_counts or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Migrated database verification failed")
        for path, raw, after in edits:
            if path.read_bytes() != raw:
                raise RuntimeError("Management file changed during migration")
            path.write_bytes(after)
            changed.append((path, raw, after))
        for old, new in renames:
            old.rename(new)
            moved.append((old, new))
        for identity in credentials:
            keyring.delete_password(OLD_SERVICE, identity)
        result["credential_count"] = len(credentials)
        (snapshot / "result.json").write_text(json.dumps({**result, "source": str(source), "destination": str(destination),
            "renamed_project_configs": [[str(a),str(b)] for a,b in moved], "edited_files": [str(p) for p,_,_ in changed],
            "snapshot_verified": True, "database_integrity": "ok"}, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    except Exception:
        for old, new in reversed(moved):
            new.rename(old)
        for path, raw, after in reversed(changed):
            if path.read_bytes() == after:
                path.write_bytes(raw)
        # Restore the database from the verified snapshot before restoring its name.
        shutil.copy2(snapshot / "application-data/manager.sqlite3", destination / "manager.sqlite3")
        destination.rename(source)
        for identity, value in credentials.items():
            keyring.set_password(OLD_SERVICE, identity, value)
        raise
    finally:
        credentials.clear()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--system-credentials", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--directory-only", action="store_true")
    parser.add_argument("--references-database", type=Path)
    parser.add_argument("--knowledge-root", type=Path)
    options = parser.parse_args()
    if options.references_database:
        if not options.knowledge_root:
            parser.error("--references-database requires --knowledge-root")
        print(json.dumps(migrate_workbench_references(options.references_database, options.source, options.destination,
            options.snapshot, options.knowledge_root, apply=options.apply), ensure_ascii=False))
        return
    keyring = __import__("keyring") if options.system_credentials else None
    result = (migrate_directory(options.source, options.destination, options.snapshot, apply=options.apply)
              if options.directory_only else migrate(options.source, options.destination, options.snapshot, workspace=options.workspace, keyring=keyring, apply=options.apply))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
