from __future__ import annotations

import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dingtalkbridge.config import Config
from dingtalkbridge.ocr import (
    DEFAULT_PROCESS_PATH, OcrError, SOURCE, _ocr_script, _run_private, run_collect,
    screen_context_record, validate_geometry,
)


def geometry():
    result = {
        "pid": 123, "hwnd": "10abc", "message_hwnd": "10abd",
        "common_ancestor_hwnd": "10aaa", "common_ancestor_class": "DingChatWnd",
        "host_class": "im_chat::DTIMChatComponent", "message_class": "CefBrowserWindow",
        "verified_process_path": DEFAULT_PROCESS_PATH, "geometry_safe": True,
        "coordinate_space": "physical_screen_pixels", "dpi_awareness": "per_monitor_v2",
        "window_rect_screen": {"x": 100, "y": 200, "width": 900, "height": 700},
        "message_window_rect_screen": {"x": 100, "y": 200, "width": 900, "height": 700},
        "message_rect_screen": {"x": 100, "y": 200, "width": 900, "height": 480},
        "composer_exclusions_screen": [{"x": 110, "y": 700, "width": 880, "height": 190}],
        "geometry_evidence": "verified_qt_chat_host+cef_common_native_ancestor+confirmed_editor_boundary+no_edit_overlap",
        "geometry_provider_errors": 0, "outside_source_unknown_ignored_count": 0,
        "composer_evidence_count": 1, "input_exclusion_count": 1,
        "capture_source_policy": "cef_message_hwnd_printwindow_only",
        "source_identity_scope": "selected_native_source_subtree",
        "source_tree_scan_complete": True, "source_tree_provider_errors": 0, "source_tree_scan_nodes": 3,
        "native_owner_proof": "validated_hwnd_or_verified_raw_ancestor_in_selected_source_subtree",
        "capture_source_process_ids": [123],
        "input_geometry_evidence": [{"provider": "uia", "reason": "edit_control", "decision": "mask",
            "control_type": "ControlType.Edit", "class_name": "SyntheticEditor", "framework_id": "Qt", "depth": 2,
            "rect": {"x": 110, "y": 700, "width": 880, "height": 190}, "is_offscreen": False,
            "source_scan": False, "native_owner_hwnd": "10abc", "owner_pid": 123,
            "owner_evidence": "explicit_native_handle"}],
        "source_native_owner_proof": [{"provider": "native_owner", "decision": "source_owner_verified",
            "native_owner_hwnd": "10abd", "owner_pid": 123, "owner_class": "CefBrowserWindow", "renderer": False,
            "source_scan": True, "native_source_descendant": True, "same_session": True,
            "verified_process_path": DEFAULT_PROCESS_PATH, "owner_visible": True}],
    }
    result["input_geometry_evidence"].extend(copy.deepcopy(result["source_native_owner_proof"]))
    return result


def observation():
    return {
        "source": SOURCE, "kind": "screen_context", "status": "available",
        "text": "SYNTHETIC OCR test evidence 123",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "line_count": 2, "word_count": 6, "confidence": 0.99,
        "language": "zh-Hans-CN", "counts_method": "recognized_text_lines_and_whitespace_tokens",
    }


class GeometryTests(unittest.TestCase):
    def test_native_contract_uses_physical_ltrb_and_hex_handles(self):
        result = validate_geometry(geometry())
        self.assertEqual(result["hwnd"], int("10abc", 16))
        self.assertEqual(result["message_rect_screen"], [100, 200, 1000, 680])
        self.assertNotIn("text", result)

    def test_fail_closed_guards(self):
        changes = [
            {"geometry_safe": False}, {"coordinate_space": "screen_pixels"},
            {"dpi_awareness": "unaware"}, {"verified_process_path": r"C:\Other\DingTalk.exe"},
            {"host_class": "OtherWindow"}, {"message_class": "Edit"},
            {"common_ancestor_class": "OtherWindow"}, {"composer_exclusions_screen": []},
            {"message_rect_screen": [100, 200, 1000, 710]},
            {"message_rect_screen": [-50, 200, 1000, 680]},
            {"message_rect_screen": [100, 200, 101, 210]},
            {"message_rect_screen": {"x": 100, "y": 200, "width": "900", "height": 480}},
            {"hwnd": True}, {"geometry_evidence": {}},
            {"source_tree_scan_complete": False}, {"source_tree_provider_errors": 1},
            {"capture_source_policy": "desktop"}, {"source_native_owner_proof": []},
            {"input_geometry_evidence": []},
        ]
        for change in changes:
            with self.subTest(change=next(iter(change))):
                candidate = geometry()
                candidate.update(change)
                with self.assertRaises(OcrError):
                    validate_geometry(candidate)

    def test_real_label_does_not_hide_detailed_owner_or_input_proof_changes(self):
        first = geometry()
        baseline = validate_geometry(first)["geometry_evidence_sha256"]
        changed = copy.deepcopy(first)
        changed["input_geometry_evidence"][0]["reason"] = "input_marker"
        self.assertEqual(changed["geometry_evidence"], first["geometry_evidence"])
        self.assertNotEqual(baseline, validate_geometry(changed)["geometry_evidence_sha256"])
        changed = copy.deepcopy(first)
        renderer = copy.deepcopy(changed["source_native_owner_proof"][0])
        renderer.update(native_owner_hwnd="10aff", owner_pid=124, owner_class="Intermediate D3D Window", renderer=True)
        changed["source_native_owner_proof"].append(renderer)
        changed["input_geometry_evidence"].append(copy.deepcopy(renderer))
        changed["capture_source_process_ids"].append(124)
        renderer_hash = validate_geometry(changed)["geometry_evidence_sha256"]
        self.assertNotEqual(baseline, renderer_hash)
        changed["source_native_owner_proof"][1]["native_owner_hwnd"] = "10afe"
        changed["input_geometry_evidence"][-1]["native_owner_hwnd"] = "10afe"
        self.assertNotEqual(renderer_hash, validate_geometry(changed)["geometry_evidence_sha256"])

    def test_review_contradictory_source_proofs_are_rejected(self):
        # Outside PID without the narrow D3D renderer allowance.
        candidate = geometry()
        extra = copy.deepcopy(candidate["source_native_owner_proof"][0])
        extra.update(native_owner_hwnd="10aff", owner_pid=124, owner_class="Chrome_WidgetWin_1", renderer=False)
        candidate["source_native_owner_proof"].append(extra)
        candidate["input_geometry_evidence"].append(copy.deepcopy(extra))
        candidate["capture_source_process_ids"].append(124)
        with self.assertRaises(OcrError):
            validate_geometry(candidate)
        # Even an outside-source strong input cannot be ignored.
        candidate = geometry()
        candidate["input_geometry_evidence"][0]["decision"] = "outside_source_tree"
        with self.assertRaisesRegex(OcrError, "strong input"):
            validate_geometry(candidate)
        candidate = geometry()
        candidate["input_geometry_evidence"][0].update(reason="unknown_value_pattern", decision="outside_source_tree", control_type="ControlType.Edit")
        with self.assertRaisesRegex(OcrError, "strong input"):
            validate_geometry(candidate)
        # A source input cannot name the right HWND with a different PID.
        candidate = geometry()
        source_input = copy.deepcopy(candidate["input_geometry_evidence"][0])
        source_input.update(source_scan=True, native_owner_hwnd="10abd", owner_pid=124)
        candidate["input_geometry_evidence"][0] = source_input
        with self.assertRaises(OcrError):
            validate_geometry(candidate)

    def test_missing_or_untyped_native_input_owner_fields_are_rejected(self):
        for field in ("native_owner_hwnd", "owner_pid", "owner_evidence", "source_scan"):
            candidate = geometry()
            del candidate["input_geometry_evidence"][0][field]
            with self.assertRaises(OcrError):
                validate_geometry(candidate)
        candidate = geometry()
        msaa = {"provider": "msaa", "role": 42, "protected": False, "depth": 2,
                "rect": {"x": 110, "y": 700, "width": 880, "height": 190}, "native_owner_hwnd": "10abc",
                "owner_pid": 123.0, "owner_class": "Qt51511QWindowIcon",
                "owner_evidence": "msaa_window_from_accessible_object", "owner_visible": True, "source_scan": False}
        candidate["input_geometry_evidence"][0] = msaa
        with self.assertRaises(OcrError):
            validate_geometry(candidate)
        candidate = geometry()
        candidate["source_native_owner_proof"][0]["renderer"] = "false"
        with self.assertRaises(OcrError):
            validate_geometry(candidate)

    def test_header_search_or_other_editable_overlap_also_blocks(self):
        candidate = geometry()
        candidate["editable_exclusions_screen"] = [[150, 210, 450, 250]]
        with self.assertRaises(OcrError):
            validate_geometry(candidate)


class ResourceTests(unittest.TestCase):
    def test_installed_helper_fallback_requires_all_three_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "data/share/dingtalkbridge/ocr"
            installed.mkdir(parents=True)
            for name in ("Capture-Ocr.ps1", "LocalOcr.ps1", "SafeWindowCrop.cs"):
                (installed / name).write_text("synthetic source", encoding="utf-8")
            with patch("dingtalkbridge.ocr.__file__", str(root / "missing/src/dingtalkbridge/ocr.py")), \
                 patch("dingtalkbridge.ocr.sysconfig.get_path", return_value=str(root / "data")):
                self.assertEqual(_ocr_script(), installed / "Capture-Ocr.ps1")
                (installed / "LocalOcr.ps1").unlink()
                with self.assertRaises(OcrError):
                    _ocr_script()


class RecordTests(unittest.TestCase):
    def test_context_has_no_inferred_message_identity_or_numeric_probability(self):
        event = observation()
        record = screen_context_record(event, validate_geometry(geometry()), "synthetic-session")
        self.assertIsNotNone(record)
        self.assertEqual(record["source"], "windows_ocr_screen")
        self.assertEqual(record["confidence"], 0)
        self.assertEqual(record["sender_id"], "unknown")
        self.assertEqual(record["metadata"]["collector_session_id"], "synthetic-session")
        self.assertFalse(record["metadata"]["is_message"])
        self.assertTrue(record["metadata"]["partial"])
        self.assertTrue(record["metadata"]["cropped"])
        self.assertEqual(record["metadata"]["timestamp_kind"], "observation_time")
        self.assertFalse(record["metadata"]["numeric_confidence_available"])
        self.assertEqual(record["metadata"]["evidence_region_counts"], {"chat_component": 2})

    def test_malformed_or_stale_observations_do_not_become_records(self):
        for changes in (
            {"status": "blank_capture"}, {"source": "other"}, {"text": " "},
            {"text": "x" * (128 * 1024 + 1)}, {"observed_at": "2000-01-01T00:00:00Z"},
            {"observed_at": "not-a-timestamp"}, {"line_count": True}, {"word_count": 0},
            {"language": "arbitrary raw UI text"}, {"counts_method": "engine_word_count"},
        ):
            event = observation()
            event.update(changes)
            self.assertIsNone(screen_context_record(event, validate_geometry(geometry()), "session"))


class SupervisorTests(unittest.TestCase):
    def _run(self, root, *, include=False, event=None, region=None, post_region=None):
        config = Config(data_dir=root)
        captured = []
        store = SimpleNamespace(path=config.database_path, ingest=lambda row: captured.append(row) or True)
        regions = {"type": "regions", "status": "available", "desktop_kind": "interactive_default",
                   "regions": [geometry() if region is None else region]}
        post_regions = copy.deepcopy(regions)
        if post_region is not None:
            post_regions["regions"] = [post_region]
        responses = [json.dumps(regions), json.dumps(event or observation()), json.dumps(post_regions)]
        with patch("dingtalkbridge.ocr.ensure_helper", return_value=root / "native.exe"), \
             patch("dingtalkbridge.ocr._ocr_script", return_value=root / "Capture-Ocr.ps1"), \
             patch("dingtalkbridge.ocr._run_private", side_effect=responses) as execute:
            result = run_collect(config, store, {"include_screen_context": include})
        return result, captured, execute.call_count

    def test_only_store_receives_text_and_summary_is_session_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result, captured, calls = self._run(root, include=True)
            self.assertEqual(calls, 3)
            self.assertEqual(len(captured), 1)
            self.assertEqual(result["ingested_screen_contexts"], 1)
            self.assertEqual(result["collector_session_id"], captured[0]["metadata"]["collector_session_id"])
            status = (root / "collector-status.json").read_text(encoding="utf-8")
            self.assertNotIn("SYNTHETIC OCR test evidence", status + json.dumps(result))
            self.assertNotIn('"text"', status)
            self.assertFalse(result["running"])
            self.assertIn("stopped_at", result)
            self.assertEqual(result["evidence_region_counts"], {"chat_component": 2})

    def test_default_does_not_store_screen_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, captured, _ = self._run(Path(temporary))
            self.assertEqual(result["status"], "available")
            self.assertEqual(captured, [])

    def test_unsafe_crop_never_launches_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = geometry()
            candidate["composer_exclusions_screen"] = []
            result, captured, calls = self._run(root, include=True, region=candidate)
            self.assertEqual(result["status"], "unsafe_geometry")
            self.assertEqual(captured, [])
            self.assertEqual(calls, 1)
            status = json.loads((root / "collector-status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "unsafe_geometry")
            self.assertNotIn("text", status)

    def test_capture_failure_has_no_ingestion(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, captured, calls = self._run(
                Path(temporary), include=True, event={"status": "blank_capture"})
            self.assertEqual(calls, 2)
            self.assertEqual(result["status"], "blank_capture")
            self.assertEqual(captured, [])

    def test_mask_or_proof_changes_discard_captured_ocr_before_store(self):
        for change in (
            {"composer_exclusions_screen": [[110, 695, 990, 890]]},
            {"geometry_evidence": {"native_display": True, "mask_generation": 2}},
            {"message_hwnd": "10abe"},
        ):
            with self.subTest(change=next(iter(change))), tempfile.TemporaryDirectory() as temporary:
                post = geometry()
                post.update(change)
                result, captured, calls = self._run(Path(temporary), include=True, post_region=post)
                self.assertEqual(calls, 3)
                self.assertEqual(captured, [])
                self.assertEqual(result["status"], "unsafe_geometry")
                self.assertFalse(result["post_capture_geometry_verified"])

    def test_database_mismatch_fails_before_any_helper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("dingtalkbridge.ocr.ensure_helper") as build:
                with self.assertRaises(OcrError):
                    run_collect(Config(data_dir=root), SimpleNamespace(path=root / "different.sqlite3"))
                build.assert_not_called()

    def test_timeout_exception_does_not_echo_partial_raw_output(self):
        class Process:
            stdin = io.StringIO()
            stdout = io.StringIO()
            stderr = None
            returncode = None

            def communicate(self, *args, **kwargs):
                raise subprocess.TimeoutExpired("helper", 1, output="synthetic-sensitive-result")

        process = Process()
        with tempfile.TemporaryDirectory() as temporary:
            with patch("dingtalkbridge.ocr.subprocess.Popen", return_value=process), \
                 patch("dingtalkbridge.ocr._stop", return_value=True):
                with self.assertRaises(OcrError) as caught:
                    _run_private(["synthetic-helper"], Path(temporary) / "stop", timeout=1)
            self.assertNotIn("synthetic-sensitive-result", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
