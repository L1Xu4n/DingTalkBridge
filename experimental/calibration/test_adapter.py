"""Pure adapter tests: no backend import, events, target query, UI, pixels or OCR."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

from BackendAdapter import safe_summary, sha256, validate_binding


class AdapterTests(unittest.TestCase):
    def test_only_known_statuses_cross_the_ui_channel(self):
        for status in ("completed", "rejected", "cancelled", "failed"):
            self.assertEqual(safe_summary({"status": status})["status"], status)
        self.assertEqual(safe_summary({"status": "available"})["status"], "failed")
        self.assertEqual(safe_summary(None)["status"], "failed")

    def test_user_content_images_and_unknown_keys_are_dropped(self):
        summary = safe_summary({"status": "completed", "screen_contexts_added": 3, "messages_added": 999, "message_text": "synthetic text",
                                "png": "synthetic bytes", "reason": "synthetic user text", "nested": {"value": "synthetic"}})
        self.assertEqual(summary, {"status": "completed", "screen_contexts_added": 3})

    def test_reason_is_an_enum_and_counts_are_bounded(self):
        self.assertEqual(safe_summary({"status": "rejected", "reason": "technical_scene_incomplete", "screen_contexts_added": -1}),
                         {"status": "rejected", "reason": "technical_scene_incomplete"})
        self.assertEqual(safe_summary({"status": "failed", "reason": "C:\\secret", "captured_text_items": 100001}), {"status": "failed"})

    def test_screen_context_counters_never_claim_verified_messages(self):
        self.assertEqual(safe_summary({"status": "completed", "ingested_screen_contexts": 1, "verified_message_count": 0}),
                         {"status": "completed", "ingested_screen_contexts": 1, "verified_message_count": 0})
        self.assertNotIn("verified_message_count", safe_summary({"status": "completed", "verified_message_count": 4}))

    def test_binding_requires_exact_local_file_paths_and_hashes(self):
        with tempfile.TemporaryDirectory(prefix="DingTalkBridge-adapter-test-") as temporary:
            directory = Path(temporary)
            config = directory / "config.json"
            config.write_text("{}", encoding="utf-8")
            adapter = Path(__file__).with_name("BackendAdapter.py").resolve()
            python = Path(sys.executable).resolve()
            binding = {"schema": "dingtalkbridge.roi-once-backend-binding.v1", "adapter_script": str(adapter),
                       "adapter_sha256": sha256(adapter), "python_exe": str(python), "python_sha256": sha256(python),
                       "config_path": str(config), "config_sha256": sha256(config)}
            path = directory / "test.binding.json"
            path.write_text(json.dumps(binding), encoding="utf-8")
            self.assertEqual(validate_binding(path, config), binding)
            config.write_text('{"changed":true}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "backend_hash_mismatch"):
                validate_binding(path, config)


if __name__ == "__main__":
    unittest.main()
