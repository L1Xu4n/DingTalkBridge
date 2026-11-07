"""Verify history normalization and archive behavior using owned synthetic records."""

from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from dingtalkbridge.config import Config
from dingtalkbridge.models import UTC, format_timestamp
from dingtalkbridge.native_history import SCHEMA, consume_history, normalize_history
from dingtalkbridge.native_receive import APP_VERSION, MODULE_SHA256, SCHEMA as LIVE_SCHEMA, consume_events
from dingtalkbridge.store import Store


class HistoryTests(unittest.TestCase):
    def setUp(self):
        """Open an isolated archive so no real account or message is used by tests."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = Config(data_dir=self.root)
        self.store = Store(self.config.database_path, self.config)
        self.now = datetime.now(UTC)

    def tearDown(self):
        """Close and remove only the temporary archive created by this test."""
        self.store.close()
        self.temporary.cleanup()

    def frame(self, cid="fixture-group", mid="101", content_type="1", content="合成正文"):
        """Build a synthetic native-service result with stable IDs and Unix milliseconds."""
        return {"schema": SCHEMA, "collector_session_id": "fixture-history", "account_id": "12345",
                "conversation_title": "合成会话", "fields": {
                    "cid": cid, "mid": mid, "sender_uid": "67890", "content": content,
                    "content_type": content_type, "msg_type": "1",
                    "create_time": str(int(self.now.timestamp() * 1000)), "update_time": "0", "status": "1"}}

    def test_group_private_and_non_text_messages_are_not_filtered_by_type(self):
        """Preserve messages from different conversation IDs and serialized non-text content."""
        for frame in (self.frame(), self.frame("fixture-private", "102", "2", '{"name":"image.png"}'),
                      self.frame("fixture-other", "103", "3", '{"fileName":"document.pdf"}')):
            self.assertEqual(consume_history(self.config, self.store, frame, "fixture-history")["inserted"], 1)
        status = self.store.status()
        self.assertEqual((status["message_count"], status["conversation_count"]), (3, 3))
        self.assertEqual(status["native_history_message_count"], 3)
        self.assertTrue(status["native_account_bound"])
        self.assertIn("document.pdf", self.store.search_messages("document.pdf")["messages"][0]["text"])

    def test_history_enriches_the_existing_live_row_without_duplication(self):
        """Use the existing native ID to replace observation time with original time and title."""
        frame = self.frame()
        live = {"schema": LIVE_SCHEMA, "app_version": APP_VERSION, "module_sha256": MODULE_SHA256,
                "collector_session_id": "fixture-live", "message_id": "101", "conversation_id": "fixture-group",
                "sender_id": "67890", "text": "合成正文", "message_timestamp": None,
                "event_observed_at": format_timestamp(self.now + timedelta(minutes=1))}
        self.assertEqual(consume_events(self.config, self.store, [live])["inserted"], 1)
        identifier = self.store.get_messages()["messages"][0]["id"]
        result = consume_history(self.config, self.store, frame, "fixture-history")
        self.assertEqual(result["updated"], 1)
        rows = self.store.get_messages()["messages"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], identifier)
        self.assertEqual(rows[0]["conversation_title"], "合成会话")
        self.assertEqual(rows[0]["metadata"]["timestamp_kind"], "client_create_time")
        self.assertEqual(consume_history(self.config, self.store, frame, "fixture-history")["duplicates_or_filtered"], 1)

    def test_account_change_rejects_history_and_live_records(self):
        """A second native account cannot write into the first account's archive."""
        consume_history(self.config, self.store, self.frame(), "fixture-history")
        other = self.frame(mid="102")
        other["account_id"] = "99999"
        with self.assertRaisesRegex(ValueError, "account changed"):
            consume_history(self.config, self.store, other, "fixture-history")
        live = {"schema": LIVE_SCHEMA, "app_version": APP_VERSION, "module_sha256": MODULE_SHA256,
                "collector_session_id": "fixture-live", "message_id": "102", "conversation_id": "fixture-group",
                "sender_id": "67890", "text": "Synthetic different-account event", "message_timestamp": None,
                "event_observed_at": format_timestamp(self.now), "account_id": "99999"}
        self.assertEqual(consume_events(self.config, self.store, [live])["storage_errors"], 1)
        live.pop("account_id")
        self.assertEqual(consume_events(self.config, self.store, [live])["destination_rejected"], 1)
        self.assertEqual(self.store.status()["message_count"], 1)

    def test_old_history_is_retained_and_conversation_policy_still_applies(self):
        """History has no age cutoff and still respects caller-selected conversations."""
        old = self.frame()
        old["fields"]["create_time"] = str(int((self.now - timedelta(days=3650)).timestamp() * 1000))
        self.assertEqual(consume_history(self.config, self.store, old, "fixture-history")["inserted"], 1)
        filtered = Config(data_dir=self.root, allowed_conversations=("another-chat",))
        self.assertEqual(consume_history(filtered, self.store, self.frame(), "fixture-history")["policy_filtered"], 1)
        self.assertEqual(self.store.status()["message_count"], 1)

    def test_empty_content_and_unknown_time_are_explicit_and_bad_sessions_are_rejected(self):
        """Keep a typed placeholder rather than dropping non-text records with no text body."""
        frame = self.frame(content_type="99", content="")
        frame["fields"]["create_time"] = "0"
        record = normalize_history(frame, "fixture-history")
        self.assertEqual(record["text"], "[消息类型 99]")
        self.assertTrue(record["metadata"]["original_message_timestamp_unknown"])
        with self.assertRaises(ValueError):
            normalize_history(frame, "wrong-session")


if __name__ == "__main__":
    unittest.main()
