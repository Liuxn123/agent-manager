from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from agent_manager.adapters.agents import AgentAdapter
from agent_manager.adapters.local import LocalHermesAdapter
from agent_manager.adapters.server import ssh_command
from agent_manager.adapters.server_worker import SERVER_PROGRAM
from agent_manager.domain import Resource, UserError
from agent_manager.runtime import ResourceLocks, TaskContext, run_process
from agent_manager.security import redact, safe_result
from agent_manager.storage import Store


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "data")

    def test_config_round_trip_merges_without_overwriting_or_exporting_tasks(self):
        resource = Resource("project", "project", {"path": str(self.root)})
        self.store.save_resource(resource)
        self.store.start_task("task1", resource.id, "test")
        exported = self.root / "config.json"
        self.store.export_config(exported)
        self.assertNotIn("tasks", json.loads(exported.read_text()))
        changed = Resource("renamed", "project", resource.options, resource.id)
        self.store.save_resource(changed)
        self.assertEqual(self.store.import_config(exported), 0)
        self.assertEqual(self.store.resources()[0].name, "renamed")
        self.store.recover_interrupted()
        self.assertEqual(self.store.tasks()[0]["state"], "interrupted")

    def test_plaintext_credentials_in_config_are_rejected_before_any_import(self):
        resource = Resource("project", "project", {"password": "test-value"})
        with self.assertRaises(UserError):
            self.store.save_resource(resource)
        exported = self.root / "bad.json"
        exported.write_text(json.dumps({"schema_version": 1, "resources": [Resource("okay", "project", {}).to_dict(), resource.to_dict()]}))
        with self.assertRaises(UserError):
            self.store.import_config(exported)
        self.assertFalse(self.store.resources())

    def test_parent_and_child_paths_are_locked_across_distinct_resources(self):
        locks = ResourceLocks()
        with locks.acquire(["resource:a", "path:" + str(self.root)]):
            with self.assertRaises(UserError):
                with locks.acquire(["resource:b", "path:" + str(self.root / "child")]):
                    pass
        with locks.acquire(["path:" + str(self.root)]):
            pass

    def test_process_output_is_bounded_and_credentials_not_in_error(self):
        with self.assertRaises(UserError) as caught:
            run_process([sys.executable, "-c", "import sys;sys.stderr.write('token=fixture-secret');sys.exit(1)"], TaskContext())
        self.assertNotIn("fixture-secret", str(caught.exception))
        with self.assertRaises(UserError):
            run_process([sys.executable, "-c", "import sys;sys.stdout.write('x'*10000000)"], TaskContext(), timeout=10)

    def test_ssh_host_injection_and_unknown_host_trust_are_rejected(self):
        for host in ["-oProxyCommand=bad", "alias;whoami", "alias name", "host\ncommand"]:
            with self.assertRaises(UserError):
                ssh_command(Resource("server", "hermes_server", {"host": host}), {"action": "observe"})
        command = ssh_command(Resource("server", "hermes_server", {"host": "my-hermes"}), {"action": "observe"})
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("BatchMode=yes", command)
        compile(SERVER_PROGRAM, "remote_worker", "exec")

    def test_agent_start_stop_only_controls_owned_process(self):
        resource = Resource("test agent", "agent", {"path": str(self.root), "executable": sys.executable,
                            "arguments": ["-c", "import time;time.sleep(60)"]})
        adapter = AgentAdapter()
        self.addCleanup(lambda: adapter.stop(resource, TaskContext()) if adapter.is_running(resource) else None)
        self.assertTrue(adapter.start(resource, TaskContext())["started"])
        self.assertTrue(adapter.is_running(resource))
        with self.assertRaises(UserError):
            adapter.start(resource, TaskContext())
        self.assertTrue(adapter.stop(resource, TaskContext())["stopped"])
        self.assertFalse(adapter.is_running(resource))
        with self.assertRaises(UserError):
            adapter.stop(Resource("unknown", "agent", {"path": str(self.root)}), TaskContext())

    def test_redaction_preserves_status_but_removes_credential_values(self):
        data = safe_result({"password": "fixture", "secrets_present": True, "result": "token=fixture-value"})
        self.assertEqual(data["password"], "[已隐藏]")
        self.assertTrue(data["secrets_present"])
        self.assertNotIn("fixture-value", data["result"])


if __name__ == "__main__":
    unittest.main()
