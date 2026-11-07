"""Core guarantees, exercised entirely with temporary synthetic test records."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import io
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from dingtalkbridge.cli import create_demo, import_jsonl, main as cli_main
from dingtalkbridge.config import Config, load_config
from dingtalkbridge.mcp import MCPServer, SUPPORTED_PROTOCOLS, serve
from dingtalkbridge.models import UTC, format_timestamp, normalize_record
from dingtalkbridge.store import Store


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.config = Config(data_dir=self.directory, db_path=self.directory / "messages.sqlite3")
        self.store = Store(self.config.database_path, self.config)
        self.now = datetime.now(UTC).replace(microsecond=0)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def record(self, index=1, **updates):
        record = {"id": f"record-{index}", "conversation_id": "project", "conversation_title": "项目",
                  "sender_id": "user-a", "sender_name": "测试用户", "text": f"第{index}条测试消息",
                  "timestamp": format_timestamp(self.now + timedelta(seconds=index)),
                  "source": "unit-test", "confidence": 1.0, "metadata": {"test": True}}
        record.update(updates)
        return record

    def request(self, method, params=None, identifier=1):
        return {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {}}

    def ready_server(self):
        server = MCPServer(self.store)
        server.handle(self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}))
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return server

    def mocked_collect_cli(self, arguments, result=None, raises=False, ocr_error_only=False):
        class ControlledError(RuntimeError):
            pass

        backend = SimpleNamespace(run_collect=Mock(return_value=result if result is not None else {"status": "available"}))
        setattr(backend, "OcrError" if ocr_error_only else "CollectorError", ControlledError)
        if raises:
            backend.run_collect.side_effect = ControlledError("PRIVATE EXCEPTION BODY")
        stdout, stderr = io.StringIO(), io.StringIO()
        imported = Mock(return_value=backend)
        with patch("dingtalkbridge.cli.load_config", return_value=self.config), \
             patch("dingtalkbridge.cli.Store") as store_class, \
             patch("dingtalkbridge.cli.importlib", SimpleNamespace(import_module=imported)), \
             patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            store_class.return_value.__enter__.return_value.dataset_kind = "local"
            code = cli_main(["collect", *arguments])
        return code, stdout.getvalue(), stderr.getvalue(), backend, imported

    def test_collect_cli_dispatches_backends_and_conditional_mode_defaults(self):
        cases = (([], ".collector", "uia", "watch"),
                 (["--backend", "ocr"], ".ocr", "ocr", "snapshot"),
                 (["--backend", "ocr", "--mode", "probe"], ".ocr", "ocr", "probe"),
                 (["--backend", "uia", "--mode", "snapshot"], ".collector", "uia", "snapshot"),
                 (["--backend", "ocr", "--mode", "watch"], ".ocr", "ocr", "watch"))
        for arguments, module_name, backend_name, mode in cases:
            with self.subTest(arguments=arguments):
                code, stdout, stderr, backend, imported = self.mocked_collect_cli(arguments)
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                self.assertEqual(json.loads(stdout)["status"], "available")
                imported.assert_called_once_with(module_name, "dingtalkbridge")
                backend.run_collect.assert_called_once()
                actual = backend.run_collect.call_args.args[2]
                self.assertEqual(actual.backend, backend_name)
                self.assertEqual(actual.mode, mode)

    def test_collect_cli_backend_errors_are_controlled_without_traceback_or_body(self):
        for arguments, ocr_error_only in (([], False), (["--backend", "ocr"], False),
                                          (["--backend", "ocr"], True)):
            with self.subTest(arguments=arguments, ocr_error_only=ocr_error_only):
                code, stdout, stderr, _, _ = self.mocked_collect_cli(arguments, raises=True, ocr_error_only=ocr_error_only)
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertNotIn("PRIVATE EXCEPTION BODY", stderr)
                self.assertNotIn("Traceback", stderr)
                self.assertIn("local collector could not complete", stderr)

    def test_collect_cli_ocr_failure_summaries_return_nonzero(self):
        failures = ("unsafe_geometry", "isolated_desktop", "collector_error", "no_text", "blank_capture",
                    "capture_unavailable", "ocr_unavailable", "target_changed", "dpi_unavailable", "stopped", "unknown")
        for status in failures:
            with self.subTest(status=status):
                code, stdout, stderr, _, _ = self.mocked_collect_cli(["--backend", "ocr"], {"status": status})
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(stdout)["status"], status)
                self.assertIn("did not produce an available observation", stderr)
                self.assertNotIn("Traceback", stderr)
        code, _, _, _, _ = self.mocked_collect_cli(["--backend", "ocr"], {"status": "stopped", "interrupted": True})
        self.assertEqual(code, 130)
        code, _, _, _, _ = self.mocked_collect_cli([], {"status": "no_visible_window"})
        self.assertEqual(code, 0)

    def test_normalization_stable_id_milliseconds_and_metadata_copy(self):
        record = self.record(timestamp=int(self.now.timestamp() * 1000))
        del record["id"]
        normalized = normalize_record(record)
        self.assertEqual(normalized["timestamp"], format_timestamp(self.now))
        self.assertEqual(normalized["id"], normalize_record(record)["id"])
        record["metadata"]["test"] = False
        self.assertTrue(normalized["metadata"]["test"])

    def test_normalization_rejects_missing_timezone_bad_confidence_and_nonjson(self):
        for changes in ({"timestamp": "2026-01-01T00:00:00"}, {"confidence": True},
                        {"confidence": float("nan")}, {"metadata": {"value": float("inf")}},
                        {"conversation_id": ""}, {"text": 123}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize_record(self.record(**changes))

    def test_dedup_by_id_and_fingerprint_does_not_change_metadata(self):
        self.assertTrue(self.store.ingest(self.record()))
        self.assertFalse(self.store.ingest(self.record(text="changed duplicate", conversation_title="wrong")))
        self.assertFalse(self.store.ingest(self.record(id="alternate-id")))
        self.assertEqual(self.store.status()["record_count"], 1)
        self.assertEqual(self.store.list_conversations()["conversations"][0]["title"], "项目")

    def test_messages_are_bounded_chronological_and_time_filtered(self):
        for index in range(1, 6):
            self.store.ingest(self.record(index))
        page = self.store.get_messages(limit=2)
        self.assertEqual([r["id"] for r in page["messages"]], ["record-4", "record-5"])
        self.assertTrue(page["has_more"])
        page = self.store.get_messages(after=self.record(2)["timestamp"], before=self.record(5)["timestamp"])
        self.assertEqual([r["id"] for r in page["messages"]], ["record-3", "record-4"])
        for value in (0, 201, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.get_messages(limit=value)

    def test_search_chinese_english_punctuation_and_sql_are_literal(self):
        texts = ["项目进度已经更新", "Unicode bridge works", "literal 100%_confirmed", "quote \" value", "normal"]
        for index, text in enumerate(texts):
            self.store.ingest(self.record(index, text=text))
        for query, expected in (("进度", 1), ("BRIDGE", 1), ("%_", 1), ('"', 1), ("' OR 1=1 --", 0)):
            with self.subTest(query=query):
                self.assertEqual(len(self.store.search_messages(query)["messages"]), expected)
        self.assertEqual(self.store.status()["record_count"], 5)

    def test_context_uses_same_conversation_and_stable_timestamp_ties(self):
        timestamp = format_timestamp(self.now)
        for index in range(1, 6):
            self.store.ingest(self.record(index, timestamp=timestamp))
        self.store.ingest(self.record(20, conversation_id="elsewhere", timestamp=timestamp))
        context = self.store.get_context("record-3", before=1, after=1)
        self.assertEqual([r["id"] for r in context["messages"]], ["record-2", "record-3", "record-4"])
        self.assertEqual(self.store.get_context("missing")["messages"], [])
        with self.assertRaises(ValueError):
            self.store.get_context("record-3", 100, 100)

    def test_cursor_polls_ingestion_order_and_empty_poll_can_resume(self):
        self.store.ingest(self.record(5))
        self.store.ingest(self.record(1))
        first = self.store.poll_events(limit=1)
        self.assertEqual(first["messages"][0]["id"], "record-5")
        self.assertTrue(first["has_more"])
        second = self.store.poll_events(cursor=first["next_cursor"], limit=1)
        self.assertEqual(second["messages"][0]["id"], "record-1")
        self.assertFalse(second["has_more"])
        empty = self.store.poll_events(cursor=second["next_cursor"])
        self.assertEqual(empty["messages"], [])
        self.store.ingest(self.record(3))
        third = self.store.poll_events(cursor=empty["next_cursor"])
        self.assertEqual(third["messages"][0]["id"], "record-3")
        for cursor in ("-1", "x", 0, "９", "9" * 19):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                self.store.poll_events(cursor=cursor)

    def test_conversation_pagination_and_all_read_paths_enforce_policy(self):
        for index, conversation in enumerate(("a", "b", "c")):
            self.store.ingest(self.record(index, conversation_id=conversation, text="find me"))
        page = self.store.list_conversations(limit=1)
        self.assertEqual(page["conversations"][0]["id"], "a")
        self.assertEqual(self.store.list_conversations(limit=1, cursor=page["next_cursor"])["conversations"][0]["id"], "b")
        self.store.close()
        self.store = Store(self.config.database_path, replace(self.config, allowed_conversations=("a", "b"), excluded_conversations=("b",)))
        self.assertEqual(self.store.status()["record_count"], 1)
        self.assertEqual(len(self.store.get_messages()["messages"]), 1)
        self.assertEqual(len(self.store.search_messages("find")["messages"]), 1)
        self.assertEqual(len(self.store.poll_events()["messages"]), 1)
        self.assertEqual(len(self.store.list_conversations()["conversations"]), 1)
        self.assertEqual(self.store.get_context("record-1")["messages"], [])
        self.assertFalse(self.store.ingest(self.record(10, conversation_id="b")))

    def test_retention_is_hidden_on_reads_but_deleted_only_on_write_or_prune(self):
        self.store.ingest(self.record())
        old = self.now - timedelta(days=40)
        self.store.connection.execute("UPDATE messages SET timestamp=?,timestamp_epoch=?", (format_timestamp(old), old.timestamp()))
        self.store.connection.commit()
        self.store.close()
        self.store = Store(self.config.database_path, self.config)
        self.assertEqual(self.store.status()["record_count"], 0)
        self.assertEqual(self.store.get_messages()["messages"], [])
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
        self.assertEqual(self.store.prune(), 1)
        self.assertFalse(self.store.ingest(self.record(2, timestamp=format_timestamp(old))))

    def test_status_reads_collector_file_and_labels_screen_context(self):
        self.assertEqual(self.store.status()["collection_state"], "not_started")
        status = {"status": "no_visible_window", "verified_message_count": 0,
                  "database_path_hash": hashlib.sha256(str(self.store.path.resolve()).casefold().encode("utf-8")).hexdigest(),
                  "running": False}
        (self.directory / "collector-status.json").write_text(json.dumps(status), encoding="utf-8")
        self.store.ingest(self.record(source="windows_uia_screen", metadata={"kind": "screen_context", "timestamp_kind": "observed_at"}))
        result = self.store.status()
        self.assertEqual(result["collector"]["last_observation_status"], "no_visible_window")
        self.assertEqual(result["collection_state"], "stopped")
        self.assertEqual(result["screen_context_count"], 1)
        self.assertEqual(result["verified_message_count"], 0)
        self.assertIn("not verified message history", result["coverage"])

    def test_screen_sources_count_records_without_claiming_messages(self):
        for index, source in enumerate(("windows_uia_screen", "windows_msaa_screen",
                                        "windows_accessibility_screen", "windows_ocr_screen")):
            self.store.ingest(self.record(index, source=source, text="Navigation label and keyboard hint",
                                          metadata={"kind": "screen_context", "is_message": False}))
        status = self.store.status()
        self.assertEqual(status["record_count"], 4)
        self.assertEqual(status["screen_context_count"], 4)
        self.assertEqual(status["message_count"], 0)
        self.assertEqual(status["verified_message_count"], 0)
        self.assertIn("navigation labels and input hints are not messages", status["coverage"])
        self.store.ingest(self.record(10, text="Explicitly imported structured test message"))
        status = self.store.status()
        self.assertEqual(status["record_count"], 5)
        self.assertEqual(status["screen_context_count"], 4)
        self.assertEqual(status["message_count"], 1)

    def test_collector_text_region_counts_are_bounded_summary_only(self):
        path = self.directory / "collector-status.json"
        db_hash = hashlib.sha256(str(self.store.path.resolve()).casefold().encode("utf-8")).hexdigest()
        status = {"status": "available", "database_path_hash": db_hash, "running": False,
                  "captured_text_items": 1_000_000,
                  "evidence_region_counts": {"chat_component": 2, "conversation_list": 3, "other": 0,
                                             "body": "PRIVATE BODY", "unknown": 99}}
        path.write_text(json.dumps(status), encoding="utf-8")
        collector = self.store.status()["collector"]
        self.assertEqual(collector["captured_text_items"], 1_000_000)
        self.assertEqual(collector["evidence_region_counts"], {"chat_component": 2, "conversation_list": 3, "other": 0})
        self.assertNotIn("PRIVATE BODY", json.dumps(collector))
        for invalid in (True, -1, 1_000_001, "2", {"body": "PRIVATE BODY"}):
            with self.subTest(invalid=invalid):
                status["captured_text_items"] = invalid
                status["evidence_region_counts"] = {"chat_component": invalid, "conversation_list": 0,
                                                     "other": invalid, "body": "PRIVATE BODY"}
                path.write_text(json.dumps(status), encoding="utf-8")
                collector = self.store.status()["collector"]
                self.assertNotIn("captured_text_items", collector)
                self.assertEqual(collector["evidence_region_counts"], {"conversation_list": 0})
                self.assertNotIn("PRIVATE BODY", json.dumps(collector))

    def test_collector_status_is_scoped_fresh_bounded_and_allowlisted(self):
        path = self.directory / "collector-status.json"
        db_hash = hashlib.sha256(str(self.store.path.resolve()).casefold().encode("utf-8")).hexdigest()
        status = {"status": "available", "database_path_hash": db_hash, "running": True,
                  "heartbeat_at": format_timestamp(datetime.now(UTC)), "interval_seconds": 5,
                  "raw_text": "PRIVATE BODY", "evidence": [{"text": "PRIVATE BODY"}],
                  "mode": "PRIVATE BODY", "verified_message_count": 999}
        path.write_text(json.dumps(status), encoding="utf-8")
        collector = self.store.status()["collector"]
        self.assertEqual(collector["status"], "recent_heartbeat")
        self.assertTrue(collector["running"])
        self.assertFalse(collector["process_liveness_verified"])
        self.assertEqual(collector["verified_message_count"], 0)
        self.assertNotIn("PRIVATE BODY", json.dumps(collector))
        status["heartbeat_at"] = format_timestamp(self.now - timedelta(minutes=2))
        path.write_text(json.dumps(status), encoding="utf-8")
        collector = self.store.status()["collector"]
        self.assertEqual(collector["status"], "stale")
        self.assertFalse(collector["running"])
        status["database_path_hash"] = "other-db"
        path.write_text(json.dumps(status), encoding="utf-8")
        self.assertEqual(self.store.status()["collector"]["status"], "not_applicable")
        path.write_bytes(b"x" * 65537)
        self.assertEqual(self.store.status()["collector"]["reason"], "status_file_too_large")

    def test_collector_session_and_ocr_coverage_are_explicit_safe_fields(self):
        path = self.directory / "collector-status.json"
        db_hash = hashlib.sha256(str(self.store.path.resolve()).casefold().encode("utf-8")).hexdigest()
        status = {"status": "available", "database_path_hash": db_hash, "running": False}
        for session in ("a", "ocr-0123456789abcdef", "20261107T120000Z_session-1", "a" * 96):
            with self.subTest(session=session):
                path.write_text(json.dumps(dict(status, collector_session_id=session)), encoding="utf-8")
                self.assertEqual(self.store.status()["collector"]["collector_session_id"], session)
        for session in ("", "a" * 97, "session with body", "session\n", "会话", "ocr/path", True, {"text": "PRIVATE BODY"}):
            with self.subTest(invalid_session=session):
                path.write_text(json.dumps(dict(status, collector_session_id=session)), encoding="utf-8")
                collector = self.store.status()["collector"]
                self.assertNotIn("collector_session_id", collector)
                self.assertNotIn("PRIVATE BODY", json.dumps(collector))
        for coverage in ("visible_accessibility_text_only", "visible_chat_display_region_ocr_only",
                         "cropped_visible_chat_component_only"):
            path.write_text(json.dumps(dict(status, coverage=coverage)), encoding="utf-8")
            self.assertEqual(self.store.status()["collector"]["coverage"], coverage)
        for coverage in ("PRIVATE BODY", {"body": "PRIVATE BODY"}):
            path.write_text(json.dumps(dict(status, coverage=coverage)), encoding="utf-8")
            self.assertNotIn("coverage", self.store.status()["collector"])

    def test_ocr_diagnostic_states_sources_and_safety_flags_are_allowlisted(self):
        path = self.directory / "collector-status.json"
        db_hash = hashlib.sha256(str(self.store.path.resolve()).casefold().encode("utf-8")).hexdigest()
        status = {"database_path_hash": db_hash, "running": False, "source": "windows_ocr_screen",
                  "cropSafety": True, "post_capture_geometry_verified": False,
                  "raw_error": "PRIVATE ERROR BODY"}
        states = ("unsafe_geometry", "blank_capture", "capture_unavailable", "ocr_unavailable", "no_text",
                  "target_changed", "dpi_unavailable", "unsupported")
        for state in states:
            with self.subTest(state=state):
                path.write_text(json.dumps(dict(status, status=state)), encoding="utf-8")
                result = self.store.status()
                collector = result["collector"]
                self.assertEqual(collector["last_observation_status"], state)
                self.assertEqual(collector["status"], "stopped")
                self.assertEqual(collector["source"], "windows_ocr_screen")
                self.assertIs(collector["cropSafety"], True)
                self.assertIs(collector["post_capture_geometry_verified"], False)
                self.assertEqual(result["record_count"], 0)
                self.assertEqual(result["verified_message_count"], 0)
                self.assertNotIn("PRIVATE ERROR BODY", json.dumps(result))
        for source in ("windows_uia_screen", "windows_msaa_screen", "windows_accessibility_screen", "windows_ocr_screen"):
            path.write_text(json.dumps(dict(status, status="available", source=source)), encoding="utf-8")
            self.assertEqual(self.store.status()["collector"]["source"], source)
        for invalid in ("PRIVATE ERROR BODY", {"body": "PRIVATE ERROR BODY"}, 1, None):
            with self.subTest(invalid=invalid):
                path.write_text(json.dumps(dict(status, status=invalid, source=invalid,
                                                cropSafety=invalid, post_capture_geometry_verified=invalid)), encoding="utf-8")
                collector = self.store.status()["collector"]
                self.assertEqual(collector["last_observation_status"], "unknown")
                self.assertNotIn("source", collector)
                self.assertNotIn("cropSafety", collector)
                self.assertNotIn("post_capture_geometry_verified", collector)
                self.assertNotIn("PRIVATE ERROR BODY", json.dumps(collector))

    def test_user_confirmed_once_real_status_contract_remains_bounded_and_nonmessage(self):
        from dingtalkbridge import ocr

        # Exercise the real summary writer with readiness closed; no UI, grant,
        # capture, OCR helper, or message text is involved in this contract test.
        with patch.object(ocr, "_once_prepare", return_value=({"ready": False}, {})), \
             patch.object(ocr, "_once_native_exchange") as capture:
            emitted = ocr.run_user_confirmed_once(self.config, self.store, SimpleNamespace())
        capture.assert_not_called()
        self.assertEqual(emitted["status"], "rejected")
        self.assertFalse(emitted["capture_started"])
        actual = self.store.status()["collector"]
        self.assertEqual(actual["last_observation_status"], "rejected")
        self.assertEqual(actual["mode"], "user_confirmed_roi_once")
        self.assertEqual(actual["collector_session_id"], emitted["collector_session_id"])
        self.assertEqual(actual["screen_contexts_added"], 0)
        path = self.directory / "collector-status.json"
        for state in ("completed", "rejected", "cancelled", "failed"):
            contract = dict(emitted, status=state, reason="PRIVATE BODY", screen_contexts_added=1,
                            ingested_screen_contexts=1, captured_text_items=2)
            path.write_text(json.dumps(contract), encoding="utf-8")
            result = self.store.status()
            collector = result["collector"]
            self.assertEqual(collector["last_observation_status"], state)
            self.assertEqual(collector["mode"], "user_confirmed_roi_once")
            self.assertEqual(collector["screen_contexts_added"], 1)
            self.assertEqual(collector["ingested_screen_contexts"], 1)
            self.assertEqual(collector["captured_text_items"], 2)
            self.assertEqual((collector["message_count"], collector["verified_message_count"]), (0, 0))
            self.assertEqual((result["record_count"], result["message_count"], result["verified_message_count"]), (0, 0, 0))
            self.assertNotIn("PRIVATE BODY", json.dumps(result))
        for invalid in (True, -1, 1_000_001, "1", {"body": "PRIVATE BODY"}):
            path.write_text(json.dumps(dict(emitted, screen_contexts_added=invalid)), encoding="utf-8")
            self.assertNotIn("screen_contexts_added", self.store.status()["collector"])

    def test_collector_desktop_status_preserves_observation_and_validates_metadata(self):
        path = self.directory / "collector-status.json"
        db_hash = hashlib.sha256(str(self.store.path.resolve()).casefold().encode("utf-8")).hexdigest()
        status = {"status": "isolated_desktop", "database_path_hash": db_hash, "running": False,
                  "desktop_kind": "isolated_sandbox", "desktop_name": "CodexSandboxDesktop",
                  "bridge_session_id": 1, "target_window_count": 4, "hidden_window_count": 4,
                  "window_count": 0}
        path.write_text(json.dumps(status), encoding="utf-8")
        collector = self.store.status()["collector"]
        self.assertEqual(collector["status"], "stopped")
        self.assertEqual(collector["last_observation_status"], "isolated_desktop")
        for key in ("desktop_kind", "desktop_name", "bridge_session_id", "target_window_count", "hidden_window_count"):
            self.assertEqual(collector[key], status[key])
        for desktop_kind in ("interactive_default", "other"):
            status.update(status="available", desktop_kind=desktop_kind, desktop_name="Default",
                          hidden_window_count=0, window_count=4)
            path.write_text(json.dumps(status), encoding="utf-8")
            collector = self.store.status()["collector"]
            self.assertEqual(collector["desktop_kind"], desktop_kind)
            self.assertEqual(collector["last_observation_status"], "available")
        for updates in ({"desktop_kind": "unknown", "bridge_session_id": True,
                         "target_window_count": -1, "hidden_window_count": "4", "desktop_name": "x" * 129},
                        {"desktop_kind": [], "bridge_session_id": 2**31,
                         "target_window_count": False, "hidden_window_count": 2**31, "desktop_name": "line\nbreak"},
                        {"desktop_name": "\ud800"}, {"desktop_name": "中" * 43}):
            with self.subTest(updates=updates):
                invalid = dict(status, **updates)
                path.write_text(json.dumps(invalid), encoding="utf-8")
                collector = self.store.status()["collector"]
                for key in updates:
                    self.assertNotIn(key, collector)

    def test_jsonl_import_is_read_only_counts_errors_without_content(self):
        path = self.directory / "input.jsonl"
        secret_text = "test-content-must-not-appear-in-import-report"
        original = "\n".join([json.dumps(self.record(text=secret_text)), json.dumps(self.record(text=secret_text)), "{bad}", "[]", ""])
        path.write_text(original, encoding="utf-8")
        result = import_jsonl(self.store, path)
        self.assertEqual((result["inserted"], result["skipped"], result["invalid"]), (1, 1, 2))
        self.assertNotIn(secret_text, json.dumps(result))
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_demo_database_is_explicit_and_rejects_real_import(self):
        result = create_demo(self.store)
        self.assertTrue(result["synthetic"])
        self.assertEqual(result["inserted"], 4)
        self.assertEqual(self.store.dataset_kind, "synthetic")
        with self.assertRaises(ValueError):
            self.store.ingest(self.record())
        with self.assertRaises(ValueError):
            import_jsonl(self.store, self.directory / "unused.jsonl")

    def test_config_validates_unknown_fields_maximum_and_environment_paths(self):
        path = self.directory / "settings.json"
        path.write_text(json.dumps({"data_dir": "%TEMP%\\bridge-test" if os.name == "nt" else "$TMPDIR/bridge-test", "max_tool_results": 12}), encoding="utf-8")
        loaded = load_config(path)
        self.assertEqual(loaded.max_tool_results, 12)
        if os.name == "nt":
            self.assertNotIn("%TEMP%", str(loaded.data_dir))
        for values in ({"unknown": 1}, {"max_tool_results": 201}, {"retention_days": True}, {"allowed_conversations": "all"}):
            path.write_text(json.dumps(values), encoding="utf-8")
            with self.subTest(values=values), self.assertRaises(ValueError):
                load_config(path)

    def test_config_explicit_then_environment_then_default_precedence(self):
        default_path = self.directory / "settings.json"
        environment_path = self.directory / "environment.json"
        explicit_path = self.directory / "explicit.json"
        for path, maximum in ((default_path, 11), (environment_path, 22), (explicit_path, 33)):
            path.write_text(json.dumps({"data_dir": str(self.directory), "max_tool_results": maximum}), encoding="utf-8")
        with patch("dingtalkbridge.config.default_data_dir", return_value=self.directory):
            with patch.dict(os.environ, {"DINGTALKBRIDGE_CONFIG": str(environment_path)}):
                self.assertEqual(load_config().max_tool_results, 22)
                self.assertEqual(load_config(explicit_path).max_tool_results, 33)
            with patch.dict(os.environ):
                os.environ.pop("DINGTALKBRIDGE_CONFIG", None)
                self.assertEqual(load_config().max_tool_results, 11)
            with patch.dict(os.environ, {"DINGTALKBRIDGE_CONFIG": str(self.directory / "missing.json")}):
                with self.assertRaisesRegex(ValueError, "selected settings file does not exist"):
                    load_config()
                self.assertEqual(load_config(explicit_path).max_tool_results, 33)

    def test_config_environment_errors_are_safe_and_never_silently_fallback(self):
        invalid_path = self.directory / "invalid.json"
        private_content = "PRIVATE CONFIG CONTENT"
        invalid_path.write_text(private_content, encoding="utf-8")
        for selected, expected in ((str(invalid_path), "valid UTF-8 JSON"),
                                   (str(self.directory), "must be a file"), ("  ", "nonempty settings path")):
            with self.subTest(selected=selected), patch.dict(os.environ, {"DINGTALKBRIDGE_CONFIG": selected}):
                with self.assertRaisesRegex(ValueError, expected) as context:
                    load_config()
                self.assertNotIn(private_content, str(context.exception))

    def test_cli_environment_config_selects_the_same_explicit_database(self):
        """Compare resolved paths so Windows 8.3 aliases identify the same selected database."""
        settings_path = self.directory / "local.json"
        data_dir = self.directory / "pinned-data"
        settings_path.write_text(json.dumps({"data_dir": str(data_dir)}), encoding="utf-8")
        environment = dict(os.environ, DINGTALKBRIDGE_CONFIG=str(settings_path))
        process = subprocess.run([sys.executable, "-m", "dingtalkbridge", "status"],
                                 text=True, encoding="utf-8", capture_output=True, timeout=10, env=environment)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(Path(json.loads(process.stdout)["database_path"]).resolve(), (data_dir / "messages.sqlite3").resolve())
        environment["DINGTALKBRIDGE_CONFIG"] = str(self.directory / "missing.json")
        process = subprocess.run([sys.executable, "-m", "dingtalkbridge", "status"],
                                 text=True, encoding="utf-8", capture_output=True, timeout=10, env=environment)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stdout, "")
        self.assertNotIn("Traceback", process.stderr)
        self.assertIn("selected settings file does not exist", process.stderr)

    def test_mcp_negotiates_three_versions_and_annotations(self):
        for version in SUPPORTED_PROTOCOLS:
            server = MCPServer(self.store)
            response = server.handle(self.request("initialize", {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}))
            self.assertEqual(response["result"]["protocolVersion"], version)
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = server.handle(self.request("tools/list"))["result"]["tools"]
        self.assertEqual(len(tools), 6)
        for tool in tools:
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])

    def test_mcp_rejects_extra_fields_bad_limits_and_unknown_tools(self):
        server = self.ready_server()
        for name, arguments in (("bridge_status", {"send": "x"}), ("get_messages", {"limit": 0}),
                                ("get_messages", {"limit": 201}), ("get_messages", {"limit": True}),
                                ("search_messages", {"query": ""}), ("get_messages", {"before": "yesterday"}),
                                ("poll_events", {"cursor": "-1"}), ("send_message", {})):
            response = server.handle(self.request("tools/call", {"name": name, "arguments": arguments}))
            with self.subTest(name=name, arguments=arguments):
                self.assertEqual(response["error"]["code"], -32602)

    def test_mcp_resource_matches_status_and_all_tools_are_callable(self):
        self.store.ingest(self.record())
        server = self.ready_server()
        for name, arguments in (("bridge_status", {}), ("list_conversations", {}), ("get_messages", {}),
                                ("search_messages", {"query": "测试"}), ("get_context", {"message_id": "record-1"}),
                                ("poll_events", {})):
            response = server.handle(self.request("tools/call", {"name": name, "arguments": arguments}))
            with self.subTest(name=name):
                self.assertFalse(response["result"]["isError"])
        resource = server.handle(self.request("resources/read", {"uri": "bridge://status"}))["result"]["contents"][0]
        self.assertEqual(json.loads(resource["text"])["record_count"], 1)
        self.assertEqual(server.handle(self.request("resources/read", {"uri": "file:///secret"}))["error"]["code"], -32002)

    def test_mcp_stdio_preserves_unicode_and_ignores_notifications(self):
        self.store.ingest(self.record(text="只读中文消息"))
        frames = [self.request("ping"),
                  self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}, 3),
                  {"jsonrpc": "2.0", "method": "notifications/initialized"},
                  self.request("tools/call", {"name": "get_messages", "arguments": {}}, 2)]
        incoming = io.StringIO("not json\n" + "\n".join(json.dumps(frame) for frame in frames) + "\n")
        outgoing = io.StringIO()
        serve(self.store, incoming, outgoing)
        responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual(len(responses), 4)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["result"], {})
        self.assertEqual(responses[3]["result"]["structuredContent"]["messages"][0]["text"], "只读中文消息")

    def test_mcp_small_config_limits_defaults_and_context(self):
        self.store.close()
        self.store = Store(self.config.database_path, replace(self.config, max_tool_results=1))
        self.store.ingest(self.record())
        server = self.ready_server()
        result = server.call_tool("get_context", {"message_id": "record-1"})
        self.assertEqual(len(result["structuredContent"]["messages"]), 1)
        self.assertEqual(len(server.call_tool("get_messages", {})["structuredContent"]["messages"]), 1)

    def test_real_cli_subprocess_stdio_has_no_banner_or_stderr(self):
        """Check the archive-only MCP protocol without launching a real collector."""
        frames = "\n".join(json.dumps(frame) for frame in (
            self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}, 1),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            self.request("tools/call", {"name": "bridge_status", "arguments": {}}, 2))) + "\n"
        process = subprocess.run([sys.executable, "-m", "dingtalkbridge", "--db", str(self.directory / "cli.sqlite3"), "serve", "--no-collector"],
                                 input=frames, text=True, encoding="utf-8", capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[1]["result"]["structuredContent"]["record_count"], 0)

    def test_mcp_lifecycle_requires_handshake_and_does_not_reinitialize(self):
        server = MCPServer(self.store)
        self.assertEqual(server.handle(self.request("tools/list"))["error"]["code"], -32000)
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(server.state, "new")
        initialize = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test"}})
        self.assertIn("result", server.handle(initialize))
        self.assertEqual(server.state, "initializing")
        self.assertEqual(server.handle(self.request("tools/list"))["error"]["code"], -32000)
        self.assertEqual(server.handle(initialize)["error"]["code"], -32600)
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(server.state, "ready")
        self.assertIn("result", server.handle(self.request("tools/list")))

    def test_response_size_cap_does_not_return_oversized_message_content(self):
        text = "x" * (128 * 1024)
        for index in range(6):
            self.store.ingest(self.record(index, text=text))
        server = self.ready_server()
        result = server.call_tool("get_messages", {"limit": 6})
        self.assertTrue(result["isError"])
        self.assertNotIn(text, json.dumps(result))
        self.assertIn("1 MiB", result["content"][0]["text"])
        self.assertFalse(server.call_tool("get_messages", {"limit": 1})["isError"])
        with self.assertRaises(ValueError):
            self.store.ingest(self.record(10, text=text + "x"))

    def test_jsonl_size_limit_is_utf8_bytes(self):
        path = self.directory / "oversized.jsonl"
        path.write_bytes((json.dumps(self.record(text="中" * 400000), ensure_ascii=False) + "\n").encode("utf-8"))
        result = import_jsonl(self.store, path)
        self.assertEqual(result["invalid"], 1)
        self.assertEqual(result["lines"], 1)

    def test_mcp_oversized_error_id_and_lone_surrogate_are_bounded(self):
        frames = [self.request("unknown", identifier="x" * (1024 * 1024 - 100)),
                  self.request("ping", identifier="\ud800"), self.request("ping", identifier=3)]
        incoming = io.StringIO("\n".join(json.dumps(frame) for frame in frames) + "\n")
        outgoing_bytes = io.BytesIO()
        outgoing = io.TextIOWrapper(outgoing_bytes, encoding="utf-8")
        serve(self.store, incoming, outgoing)
        data = outgoing_bytes.getvalue()
        responses = [json.loads(line) for line in data.splitlines()]
        self.assertTrue(all(len(line) + 1 <= 1024 * 1024 for line in data.splitlines()))
        self.assertIsNone(responses[0]["id"])
        self.assertIsNone(responses[1]["id"])
        self.assertEqual(responses[2]["id"], 3)
        self.assertEqual(responses[2]["result"], {})

    def test_deep_json_is_rejected_without_terminating_stdio_or_import(self):
        deep = "[" * 10000 + "0" + "]" * 10000 + "\n"
        incoming = io.StringIO(deep + json.dumps(self.request("ping")) + "\n")
        outgoing = io.StringIO()
        serve(self.store, incoming, outgoing)
        responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["result"], {})
        path = self.directory / "deep.jsonl"
        path.write_text(deep + json.dumps(self.record()) + "\n", encoding="utf-8")
        result = import_jsonl(self.store, path)
        self.assertEqual((result["invalid"], result["inserted"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
