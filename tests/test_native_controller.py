"""Mock-process supervision only: no executable, hook, pipe, or target is run."""

from dataclasses import replace
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dingtalkbridge import native_controller as supervisor
from dingtalkbridge.cli import main as cli_main
from dingtalkbridge.config import Config
from dingtalkbridge.models import UTC, format_timestamp
from dingtalkbridge.native_receive import APP_VERSION, MODULE_SHA256, REAL_SOURCE, SCHEMA
from dingtalkbridge.store import Store


class FakeInput:
    def __init__(self, on_close=lambda: None):
        """Fixture helper:   init  ."""
        self.data = b""
        self.closed = False
        self.on_close = on_close

    def write(self, value):
        """Fixture helper: write."""
        self.data += value

    def flush(self):
        """Fixture helper: flush."""
        pass

    def close(self):
        """Fixture helper: close."""
        self.closed = True
        self.on_close()


class WaitingOutput:
    def __init__(self, prefix=b""):
        """Fixture helper:   init  ."""
        self.prefix = io.BytesIO(prefix)
        self.stopped = threading.Event()

    def readline(self, size):
        """Fixture helper: readline."""
        value = self.prefix.readline(size)
        if value:
            return value
        self.stopped.wait(3)
        return b""

    def close(self):
        """Fixture helper: close."""
        self.stopped.set()


class FakeProcess:
    def __init__(self, output, exit_code=0, hang_stop=False):
        """Fixture helper:   init  ."""
        self.stdout = io.BytesIO(output) if isinstance(output, bytes) else output
        self.stdin = FakeInput(getattr(self.stdout, "close", lambda: None) if isinstance(self.stdout, WaitingOutput) else lambda: None)
        self.returncode = None
        self.exit_code = exit_code
        self.hang_stop = hang_stop
        self.terminated = False
        self.killed = False

    def poll(self):
        """Fixture helper: poll."""
        return self.returncode

    def wait(self, timeout=None):
        """Fixture helper: wait."""
        if self.hang_stop and not self.terminated and not self.killed:
            raise supervisor.subprocess.TimeoutExpired("synthetic-process", timeout)
        self.returncode = -15 if self.terminated else self.exit_code
        return self.returncode

    def terminate(self):
        """Fixture helper: terminate."""
        self.terminated = True
        if isinstance(self.stdout, WaitingOutput):
            self.stdout.close()

    def kill(self):
        """Fixture helper: kill."""
        self.killed = True


class NativeControllerTests(unittest.TestCase):
    def setUp(self):
        """Fixture helper: setUp."""
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.config = Config(data_dir=self.directory / "data", db_path=self.directory / "data" / "messages.sqlite3")
        self.store = Store(self.config.database_path, self.config)
        self.production = self.make_build(synthetic=False)
        self.synthetic = self.make_build(synthetic=True)
        self.session = "native-controller-fixture"

    def tearDown(self):
        """Fixture helper: tearDown."""
        self.store.close()
        self.temporary.cleanup()

    def make_build(self, synthetic):
        """Fixture helper: make build."""
        path = self.directory / ("synthetic" if synthetic else "production")
        path.mkdir()
        executable = path / ("BridgeControllerSynthetic.exe" if synthetic else "BridgeController.exe")
        dll = path / ("BridgeBootstrapSynthetic.dll" if synthetic else "BridgeBootstrap.dll")
        executable.write_bytes(b"not-an-executable; never launched by tests")
        dll.write_bytes(b"not-a-dll; never loaded by tests")
        target = path / "DingTalkBridgeNativeSyntheticHost.exe"
        manifest = {"schema": supervisor.BUILD_SCHEMA, "flavor": "synthetic" if synthetic else "production",
                    "synthetic_core": "root", "mainframe_sha256": MODULE_SHA256,
                    "target_path": str(target) if synthetic else r"C:\DingTalk\DingTalk.exe",
                    "controller_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                    "dll_sha256": hashlib.sha256(dll.read_bytes()).hexdigest()}
        (path / "build.json").write_text(json.dumps(manifest), encoding="utf-8")
        return executable

    def status(self, event="active", *, synthetic=False, **changes):
        """Fixture helper: status."""
        stopped = event == "stopped"
        result = {"type": supervisor.STATUS_TYPE, "event": event, "status": 5 if stopped else 4,
                  "error": 0, "active": 0 if stopped else 1, "worker_alive": 0 if stopped else 1,
                  "build_flavor": 2 if synthetic else 1, "callbacks_seen": 7, "notifications_400": 2,
                  "messages_seen": 2, "messages_sent": 1}
        result.update(changes)
        return result

    def event(self, **changes):
        """Fixture helper: event."""
        event = {"schema": SCHEMA, "app_version": APP_VERSION, "module_sha256": MODULE_SHA256,
                 "collector_session_id": self.session, "message_id": "native-fixture-message",
                 "conversation_id": "native-fixture-conversation", "sender_id": "fixture-sender",
                 "text": "PRIVATE TEST BODY: 测试正文", "message_timestamp": None,
                 "event_observed_at": format_timestamp(datetime.now(UTC))}
        event.update(changes)
        return event

    def lines(self, *frames):
        """Fixture helper: lines."""
        return b"".join(json.dumps(frame, ensure_ascii=False).encode("utf-8") + b"\n" for frame in frames)

    def run_mock(self, frames, *, mode="capture", synthetic=False, duration=0, process=None):
        """Fixture helper: run mock."""
        args = SimpleNamespace(pid=123, hwnd="0x1234", duration=duration, mode=mode, session=self.session,
                               controller=self.synthetic if synthetic else self.production, synthetic=synthetic)
        fake = process or FakeProcess(frames)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(supervisor, "IS_WINDOWS", True), patch.object(supervisor.subprocess, "Popen", return_value=fake) as popen, \
             patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            result = supervisor.run_collect(self.config, self.store, args)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        return result, fake, popen

    def test_production_capture_private_roundtrip_remains_unverified(self):
        """Verify production capture private roundtrip remains unverified."""
        result, process, popen = self.run_mock(self.lines(self.status(), self.event(), self.status("heartbeat"), self.status("stopped")))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["native_counts"]["inserted"], 1)
        self.assertEqual(result["source"], REAL_SOURCE)
        self.assertEqual(result["collector_session_id"], self.session)
        self.assertEqual((result["message_count"], result["verified_message_count"]), (1, 0))
        self.assertEqual(result["account_identity"], "unknown")
        self.assertFalse(result["native_callback_verified"])
        self.assertNotIn(self.event()["text"], json.dumps(result, ensure_ascii=False))
        record = self.store.get_messages()["messages"][0]
        self.assertEqual(record["source"], REAL_SOURCE)
        self.assertEqual(record["metadata"]["structured_verification"], "pending")
        self.assertTrue(record["metadata"]["original_message_timestamp_unknown"])
        command = popen.call_args.args[0]
        self.assertIn("--stream", command)
        self.assertEqual(command[command.index("--hwnd") + 1], str(0x1234))
        self.assertEqual(popen.call_args.kwargs["stdout"], supervisor.subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs["stderr"], supervisor.subprocess.DEVNULL)
        self.assertFalse(popen.call_args.kwargs["text"])
        self.assertEqual(process.stdin.data, b"stop\n")
        status = self.store.status()["collector"]
        self.assertEqual(status["last_observation_status"], "completed")
        self.assertEqual(status["mode"], "native_capture")
        self.assertEqual(status["native_counts"]["inserted"], 1)
        self.assertFalse(status["native_callback_verified"])

    def test_count_default_emits_no_stream_argument_and_never_archives_dto(self):
        """Verify count default emits no stream argument and never archives dto."""
        result, _, popen = self.run_mock(self.lines(self.status(), self.status("stopped")), mode="count")
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("--stream", popen.call_args.args[0])
        self.assertEqual(self.store.status()["record_count"], 0)
        result, _, _ = self.run_mock(self.lines(self.status(), self.event()), mode="count")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "unexpected_data_frame")
        self.assertEqual(self.store.status()["record_count"], 0)

    def test_synthetic_envelope_requires_both_explicit_flavor_and_synthetic_store(self):
        """Verify synthetic envelope requires both explicit flavor and synthetic store."""
        self.store.mark_synthetic()
        envelope = {"schema": supervisor.SYNTHETIC_ENVELOPE, "event": self.event()}
        result, _, _ = self.run_mock(self.lines(self.status(synthetic=True), envelope, self.status("stopped", synthetic=True)), synthetic=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["native_counts"]["inserted"], 1)
        record = self.store.get_messages()["messages"][0]
        self.assertEqual(record["source"], "synthetic")
        self.assertTrue(record["metadata"]["synthetic"])
        self.assertFalse(record["metadata"]["native_callback_verified"])
        result, _, _ = self.run_mock(self.lines(self.status(synthetic=True), self.event()), synthetic=True)
        self.assertEqual(result["failure_reason"], "synthetic_envelope_required")

    def test_production_rejects_synthetic_envelope_and_mismatched_session(self):
        """Verify production rejects synthetic envelope and mismatched session."""
        for event in ({"schema": supervisor.SYNTHETIC_ENVELOPE, "event": self.event()}, self.event(collector_session_id="other-session")):
            result, _, _ = self.run_mock(self.lines(self.status(), event))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(self.store.status()["record_count"], 0)

    def test_build_and_destination_mismatch_fail_before_mock_process_launch(self):
        """Verify build and destination mismatch fail before mock process launch."""
        args = SimpleNamespace(pid=123, hwnd=321, controller=self.production)
        for changes in ({"synthetic": True}, {"pid": 0}, {"hwnd": -1}, {"duration": float("nan")},
                        {"duration": 3601}, {"session": "bad body"}):
            with self.subTest(changes=changes), patch.object(supervisor, "IS_WINDOWS", True), patch.object(supervisor.subprocess, "Popen") as popen:
                with self.assertRaises(supervisor.CollectorError):
                    supervisor.run_collect(self.config, self.store, dict(vars(args), **changes))
                popen.assert_not_called()
        self.production.write_bytes(b"modified controller must not run")
        with patch.object(supervisor, "IS_WINDOWS", True), patch.object(supervisor.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(supervisor.CollectorError, "build_artifact_mismatch"):
                supervisor.run_collect(self.config, self.store, args)
            popen.assert_not_called()
        with patch.object(supervisor, "IS_WINDOWS", True), patch.object(supervisor.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(supervisor.CollectorError, "database_mismatch"):
                supervisor.run_collect(replace(self.config, db_path=self.directory / "other.sqlite3"), self.store, args)
            popen.assert_not_called()

    def test_startup_and_heartbeat_timeouts_request_cooperative_stop(self):
        """Verify startup and heartbeat timeouts request cooperative stop."""
        process = FakeProcess(WaitingOutput())
        with patch.object(supervisor, "STARTUP_TIMEOUT_SECONDS", 0.01):
            result, process, _ = self.run_mock(b"", process=process)
        self.assertEqual(result["failure_reason"], "startup_timeout")
        self.assertEqual(process.stdin.data, b"stop\n")
        process = FakeProcess(WaitingOutput(self.lines(self.status())))
        with patch.object(supervisor, "HEARTBEAT_TIMEOUT_SECONDS", 0.01):
            result, process, _ = self.run_mock(b"", process=process)
        self.assertEqual(result["failure_reason"], "heartbeat_timeout")
        self.assertTrue(result["graceful_stop"])

    def test_duration_deadline_stops_controller_and_fallback_is_not_reported_graceful(self):
        """Verify duration deadline stops controller and fallback is not reported graceful."""
        process = FakeProcess(WaitingOutput(self.lines(self.status())))
        result, process, _ = self.run_mock(b"", process=process, duration=0.01)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(process.stdin.data, b"stop\n")
        process = FakeProcess(self.lines(self.status(), self.status("stopped")), hang_stop=True)
        result, process, _ = self.run_mock(b"", process=process)
        self.assertTrue(process.terminated)
        self.assertFalse(result["graceful_stop"])
        self.assertEqual(result["status"], "failed")

    def test_invalid_status_json_oversize_and_dto_never_escape_as_raw_output(self):
        """Verify invalid status json oversize and dto never escape as raw output."""
        invalid_frames = [self.lines(self.status(text="PRIVATE STATUS BODY")),
                          self.lines(self.status(build_flavor=2)), self.lines(self.status(error=True)),
                          self.lines(self.status(), self.event(app_version="")),
                          self.lines(self.event()), b"not json PRIVATE BODY\n",
                          b"x" * supervisor.MAX_FRAME_BYTES + b"\n", self.lines(self.status())[:-1]]
        for frames in invalid_frames:
            with self.subTest(size=len(frames)):
                result, _, _ = self.run_mock(frames)
                self.assertEqual(result["status"], "failed")
                self.assertNotIn("PRIVATE", json.dumps(result))
                self.assertEqual(self.store.status()["record_count"], 0)

    def test_output_reader_queue_is_bounded_and_detects_backpressure(self):
        """Verify output reader queue is bounded and detects backpressure."""
        reader = supervisor._PrivateOutput(io.BytesIO(self.lines(*[self.status()] * 100)))
        reader.start()
        reader.thread.join(timeout=1)
        self.assertLessEqual(reader.items.qsize(), supervisor.QUEUE_CAPACITY)
        self.assertTrue(reader.overflow.is_set())
        self.assertTrue(reader.done.is_set())

    def test_keyboard_interrupt_requests_stop_and_nonzero_exit_is_failure(self):
        """Verify keyboard interrupt requests stop and nonzero exit is failure."""
        output = SimpleNamespace(items=SimpleNamespace(get=Mock(side_effect=KeyboardInterrupt)),
                                 overflow=threading.Event(), done=threading.Event(), stop=threading.Event(), start=Mock())
        with patch.object(supervisor, "_PrivateOutput", return_value=output):
            result, process, _ = self.run_mock(b"")
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(result["interrupted"])
        self.assertEqual(process.stdin.data, b"stop\n")
        process = FakeProcess(self.lines(self.status(), self.status("stopped")), exit_code=7)
        result, _, _ = self.run_mock(b"", process=process)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "controller_exit_failed")

    def test_status_lifecycle_requires_active_then_stopped_without_reactivation(self):
        """Verify status lifecycle requires active then stopped without reactivation."""
        sequences = ((self.status("heartbeat"), self.status("stopped")),
                     (self.status(), self.status("stopped"), self.status()),
                     (self.status(), self.status("stopped", status=4)))
        for sequence in sequences:
            result, _, _ = self.run_mock(self.lines(*sequence))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(self.store.status()["record_count"], 0)

    def test_native_status_counter_allowlist_never_echoes_unknown_keys_or_claims_verified(self):
        """Verify native status counter allowlist never echoes unknown keys or claims verified."""
        raw = {"database_path_hash": hashlib.sha256(str(self.store.path.resolve()).casefold().encode("utf-8")).hexdigest(),
               "status": "completed", "mode": "native_capture", "source": REAL_SOURCE,
               "coverage": "native_event_dto_pending", "native_callback_verified": True,
               "native_counts": {"inserted": 2, "invalid": True, "text": "PRIVATE BODY"},
               "native_controller_counts": {"status": 5, "error": 0, "active": 2, "messages_seen": 5,
                                            "body": "PRIVATE BODY", "messages_sent": 2**63}, "running": False}
        (self.config.data_dir / "collector-status.json").write_text(json.dumps(raw), encoding="utf-8")
        collector = self.store.status()["collector"]
        self.assertEqual(collector["native_counts"], {"inserted": 2})
        self.assertEqual(collector["native_controller_counts"], {"status": 5, "error": 0, "messages_seen": 5})
        self.assertFalse(collector["native_callback_verified"])
        self.assertEqual(collector["verified_message_count"], 0)
        self.assertNotIn("PRIVATE BODY", json.dumps(collector))

    def test_native_cli_routes_count_through_discovered_window(self):
        """The production command resolves the window before calling the collector."""
        stdout = io.StringIO()
        with patch("dingtalkbridge.cli.load_config", return_value=self.config), \
             patch("dingtalkbridge.native_target.select_target", return_value={"pid": 123, "hwnd": 456}), \
             patch.object(supervisor, "run_collect", return_value={"status": "completed"}) as collect, \
             patch("sys.stdout", stdout):
            code = cli_main(["native", "--mode", "count"])
        self.assertEqual(code, 0)
        self.assertEqual(collect.call_args.args[2].hwnd, 456)
        self.assertEqual(collect.call_args.args[2].mode, "count")

    def test_native_stop_command_requests_cooperative_shutdown(self):
        """Stopping writes the bridge request file without terminating the client."""
        with patch("dingtalkbridge.cli.load_config", return_value=self.config), patch("sys.stdout", io.StringIO()):
            code = cli_main(["native", "--mode", "stop"])
        self.assertEqual(code, 0)
        self.assertTrue((self.config.data_dir / "native-stop.request").exists())


if __name__ == "__main__":
    unittest.main()
