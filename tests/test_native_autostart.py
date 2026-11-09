"""Check MCP startup ownership and protocol isolation without attaching to DingTalk."""

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from dingtalkbridge import autostart
from dingtalkbridge.cli import main
from dingtalkbridge.config import Config
from dingtalkbridge.native_controller import _acquire_lock, request_stop
from dingtalkbridge.store import Store


@unittest.skipUnless(os.name == "nt", "Windows collector locks")
class AutostartTests(unittest.TestCase):
    def setUp(self):
        """Create an isolated archive and effective policy for each startup scenario."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = Config(data_dir=self.root, db_path=self.root / "selected.sqlite3",
                             allowed_conversations=("selected",), excluded_conversations=("excluded",),
                             max_tool_results=20)

    def tearDown(self):
        """Remove only this test's temporary files after all locks have closed."""
        self.temporary.cleanup()

    def test_reuses_manual_collector_or_waiting_autostart_worker(self):
        """Both a running collector and a waiting worker prevent another launch."""
        for name in ("native-collector.lock", "native-autostart.lock"):
            with self.subTest(name=name):
                handle = _acquire_lock(self.config, name)
                try:
                    with patch.object(autostart.subprocess, "Popen") as spawn:
                        self.assertEqual(autostart.ensure_collector(self.config)["status"], "already_running")
                        spawn.assert_not_called()
                finally:
                    handle.close()

    def test_launch_uses_private_io_and_preserves_effective_configuration(self):
        """An idle lock file permits startup; overrides and filters reach the child intact."""
        _acquire_lock(self.config).close()
        process = Mock(pid=123, stdin=Mock())
        stdout = io.StringIO()
        with patch.object(autostart, "_validate_build"), \
             patch.object(autostart.subprocess, "Popen", return_value=process) as spawn, \
             patch("sys.stdout", stdout):
            result = autostart.ensure_collector(self.config)
        self.assertEqual(result["status"], "starting")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(spawn.call_args.kwargs["stdout"], autostart.subprocess.DEVNULL)
        self.assertEqual(spawn.call_args.kwargs["stderr"], autostart.subprocess.DEVNULL)
        self.assertEqual(spawn.call_args.kwargs["startupinfo"].lpDesktop, "winsta0\\default")
        settings = json.loads(process.stdin.write.call_args.args[0])
        self.assertEqual(settings["db_path"], str(self.config.database_path.resolve()))
        self.assertEqual(settings["allowed_conversations"], ["selected"])
        self.assertEqual(settings["excluded_conversations"], ["excluded"])
        self.assertNotIn("retention_days", settings)
        self.assertNotIn("history_max_conversations", settings)
        self.assertNotIn("history_messages_per_conversation", settings)
        process.stdin.close.assert_called_once()

    def test_waits_for_client_then_starts_capture(self):
        """Opening DingTalk after MCP startup starts collection without another MCP connection."""
        target = {"pid": 123, "hwnd": 456}

        def collect_until_stop(*_args):
            """End the persistent worker through its normal explicit stop request."""
            request_stop(self.config)
            return {"status": "completed"}

        with patch.object(autostart, "_validate_build"), \
             patch.object(autostart, "select_target", side_effect=[ValueError("not open"), target]), \
             patch.object(autostart.time, "sleep"), \
             patch.object(autostart, "run_collect", side_effect=collect_until_stop) as collect:
            self.assertEqual(autostart.run_worker(self.config), 0)
        self.assertEqual(collect.call_args.args[2]["mode"], "capture")
        self.assertEqual(collect.call_args.args[2]["duration"], 0)
        self.assertTrue(collect.call_args.args[2]["preserve_stop_request"])
        self.assertFalse(autostart._locked(self.config, "native-autostart.lock"))

    def test_native_rejection_waits_for_new_client_then_recovers(self):
        """Avoid retrying a resident failed hook and resume when the user reopens DingTalk."""
        targets = [{"pid": 123, "hwnd": 456}, {"pid": 123, "hwnd": 789}, {"pid": 124, "hwnd": 890}]

        def collect(_config, _store, options):
            """Fail the old client once, then stop after the new client's successful run."""
            if options["pid"] == 123:
                return {"status": "failed", "running": False, "requires_client_restart": True}
            request_stop(self.config)
            return {"status": "completed"}

        with patch.object(autostart, "_validate_build"), \
             patch.object(autostart, "select_target", side_effect=targets), \
             patch.object(autostart.time, "sleep"), \
             patch.object(autostart, "run_collect", side_effect=collect) as capture:
            self.assertEqual(autostart.run_worker(self.config), 0)
        self.assertEqual([call.args[2]["pid"] for call in capture.call_args_list], [123, 124])

    def test_mcp_monitor_recovers_worker_and_respects_manual_stop(self):
        """Recheck an idle MCP server once, then suppress restart after Stop-Native."""
        event = Mock()
        event.wait.side_effect = [False, False, True]

        def ensure(_config, **_options):
            """Simulate a recovered worker followed by an explicit user stop."""
            if launch.call_count == 2:
                request_stop(self.config)

        with patch.object(autostart, "ensure_collector", side_effect=ensure) as launch, \
             patch.object(autostart.threading, "Event", return_value=event), \
             patch.object(autostart.threading, "Thread") as thread:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
            with autostart.maintain_collector(self.config):
                pass
        self.assertEqual(launch.call_count, 2)
        event.set.assert_called_once()

    def test_stop_while_waiting_never_installs_hook(self):
        """Stop-Native also stops a worker that is still waiting for DingTalk."""
        with patch.object(autostart, "_validate_build"), \
             patch.object(autostart, "select_target", side_effect=ValueError("not open")), \
             patch.object(autostart.time, "sleep", side_effect=lambda _: request_stop(self.config)), \
             patch.object(autostart, "run_collect") as collect:
            self.assertEqual(autostart.run_worker(self.config), 0)
            self.assertEqual(autostart.run_worker(self.config), 0)
        collect.assert_not_called()
        status = json.loads((self.root / "collector-status.json").read_text())
        self.assertEqual(status["status"], "stopped")
        self.assertFalse(status["running"])

    def test_serve_starts_by_default_and_supports_archive_only(self):
        """Existing serve configurations autostart; explicit archive-only usage does not."""
        for arguments, expected in ((["serve"], 1), (["serve", "--no-collector"], 0), (["--demo-data", "serve"], 0)):
            with patch("dingtalkbridge.cli.load_config", return_value=self.config), \
                 patch.object(autostart, "ensure_collector") as ensure, \
                 patch("sys.stdin", io.StringIO()), patch("sys.stdout", io.StringIO()):
                self.assertEqual(main(arguments), 0)
                self.assertEqual(ensure.call_count, expected)
        with Store(self.config.database_path, self.config) as store:
            store.mark_synthetic()
        with patch("dingtalkbridge.cli.load_config", return_value=self.config), \
             patch.object(autostart, "ensure_collector") as ensure, \
             patch("sys.stdin", io.StringIO()), patch("sys.stdout", io.StringIO()):
            self.assertEqual(main(["serve"]), 0)
            ensure.assert_not_called()

    def test_launch_failure_keeps_mcp_available_and_stdout_clean(self):
        """A missing native build cannot corrupt or prevent the MCP initialize reply."""
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("dingtalkbridge.cli.load_config", return_value=self.config), \
             patch.object(autostart, "ensure_collector", side_effect=ValueError("PRIVATE DETAIL")), \
             patch("sys.stdin", io.StringIO(json.dumps(request) + "\n")), \
             patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            self.assertEqual(main(["serve"]), 0)
        self.assertIn("result", json.loads(stdout.getvalue()))
        self.assertNotIn("PRIVATE DETAIL", stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
