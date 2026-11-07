"""Synthetic DTO/Store tests only; no native process, transport, or callback."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dingtalkbridge.config import Config
from dingtalkbridge.models import MAX_TEXT_BYTES, UTC, format_timestamp
from dingtalkbridge.native_receive import (
    APP_VERSION, EVENT_SCHEMA, MAX_FRAME_BYTES, MODULE_SHA256, REAL_SOURCE, SCHEMA,
    NativeReceiveError, consume_events, make_synthetic_event,
)
from dingtalkbridge.store import Store


class NativeReceiveTests(unittest.TestCase):
    def setUp(self):
        """Fixture helper: setUp."""
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.config = Config(data_dir=self.directory, db_path=self.directory / "native-fixture.sqlite3")
        self.store = Store(self.config.database_path, self.config)
        self.now = datetime.now(UTC).replace(microsecond=0)

    def tearDown(self):
        """Fixture helper: tearDown."""
        self.store.close()
        self.temporary.cleanup()

    def event(self, **changes):
        # Plain fixtures exercise the future real-DTO branch in a temporary DB;
        # they are not produced by, or evidence of, an actual native callback.
        """Fixture helper: event."""
        event = {
            "schema": SCHEMA, "app_version": APP_VERSION, "module_sha256": MODULE_SHA256,
            "collector_session_id": "fixture-session-1", "message_id": "fixture-message-1",
            "conversation_id": "fixture-conversation", "sender_id": "fixture-sender",
            "text": "Synthetic DTO content: 测试正文 only.",
            "message_timestamp": format_timestamp(self.now - timedelta(minutes=1)),
            "event_observed_at": format_timestamp(self.now),
        }
        event.update(changes)
        return event

    def test_strict_schema_has_only_the_agreed_discriminator_and_data_fields(self):
        """Verify strict schema has only the agreed discriminator and data fields."""
        expected = {"schema", "app_version", "module_sha256", "collector_session_id", "message_id",
                    "conversation_id", "sender_id", "text", "message_timestamp", "event_observed_at"}
        self.assertEqual(set(EVENT_SCHEMA["required"]), expected)
        self.assertEqual(set(EVENT_SCHEMA["properties"]), expected | {"account_id"})
        self.assertFalse(EVENT_SCHEMA["additionalProperties"])
        self.assertEqual(EVENT_SCHEMA["properties"]["schema"]["const"], SCHEMA)
        self.assertNotIn("const", EVENT_SCHEMA["properties"]["app_version"])
        self.assertEqual(EVENT_SCHEMA["properties"]["message_timestamp"]["type"], ["string", "null"])

    def test_normalized_record_is_pending_without_invented_names_or_identity(self):
        """Verify normalized record is pending without invented names or identity."""
        event = self.event(sender_id="")
        counts = consume_events(self.config, self.store, [event])
        self.assertEqual((counts["received"], counts["inserted"]), (1, 1))
        record = self.store.get_messages()["messages"][0]
        self.assertEqual(record["text"], event["text"])
        self.assertEqual(record["timestamp"], event["message_timestamp"])
        self.assertEqual(record["source"], REAL_SOURCE)
        self.assertEqual(record["confidence"], 0)
        self.assertEqual((record["sender_name"], record["conversation_title"], record["sender_id"]), ("", "", ""))
        metadata = record["metadata"]
        self.assertEqual(metadata["kind"], "native_event")
        self.assertEqual(metadata["account_identity"], "unknown")
        self.assertEqual(metadata["structured_verification"], "pending")
        self.assertFalse(metadata["native_callback_verified"])
        self.assertFalse(metadata["synthetic"])
        self.assertFalse(metadata["original_message_timestamp_unknown"])
        self.assertEqual(metadata["timestamp_kind"], "producer_claimed_message_time")
        self.assertEqual(metadata["event_observed_at"], event["event_observed_at"])
        self.assertEqual(metadata["native_message_id"], event["message_id"])
        self.assertNotIn(event["text"], json.dumps(metadata, ensure_ascii=False))

    def test_utf8_bytes_json_string_and_dict_frames_share_deduplication(self):
        """Verify utf8 bytes json string and dict frames share deduplication."""
        event = self.event()
        json_frame = json.dumps(event, ensure_ascii=False)
        counts = consume_events(self.config, self.store, [event, json_frame, json_frame.encode("utf-8")])
        self.assertEqual(counts["received"], 3)
        self.assertEqual(counts["inserted"], 1)
        self.assertEqual(counts["duplicates_or_filtered"], 2)
        self.assertEqual(self.store.status()["record_count"], 1)

    def test_native_id_is_stable_across_sessions_but_scoped_to_conversation(self):
        """Verify native id is stable across sessions but scoped to conversation."""
        counts = consume_events(self.config, self.store, [self.event(), self.event(collector_session_id="fixture-session-2"),
                                                        self.event(conversation_id="second-conversation")])
        self.assertEqual(counts["inserted"], 2)
        self.assertEqual(counts["duplicates_or_filtered"], 1)
        records = self.store.get_messages()["messages"]
        self.assertNotEqual(records[0]["id"], records[1]["id"])
        self.assertEqual(records[0]["metadata"]["collector_session_id"], "fixture-session-1")

    def test_invalid_schema_extra_fields_and_missing_fields_are_rejected(self):
        """Verify invalid schema extra fields and missing fields are rejected."""
        events = [self.event(app_version=""), self.event(module_sha256="0" * 65),
                  self.event(schema="another.protocol"),
                  self.event(source="synthetic"), self.event(metadata={"body": "PRIVATE"})]
        for name in self.event():
            event = self.event()
            del event[name]
            events.append(event)
        counts = consume_events(self.config, self.store, events)
        self.assertEqual(counts["invalid"], len(events))
        self.assertEqual(counts["inserted"], 0)
        self.assertEqual(self.store.status()["record_count"], 0)

    def test_other_client_version_and_no_hash_are_accepted(self):
        """Compatible clients are not rejected solely for version or hash differences."""
        counts = consume_events(self.config, self.store, [self.event(app_version="9.1.0.123", module_sha256="")])
        self.assertEqual(counts["inserted"], 1)
        self.assertEqual(self.store.get_messages()["messages"][0]["metadata"]["app_version"], "9.1.0.123")

    def test_all_fields_are_typed_and_ids_and_sessions_are_bounded(self):
        """Verify all fields are typed and ids and sessions are bounded."""
        changes = [{name: None} for name in self.event() if name != "message_timestamp"]
        changes += [{"message_id": ""}, {"conversation_id": " "}, {"sender_id": "user\nname"},
                    {"conversation_id": " padded "}, {"collector_session_id": "session with body"},
                    {"collector_session_id": "x" * 97}, {"collector_session_id": "session\n"},
                    {"sender_id": "中" * 1366}, {"text": "\ud800"}, {"text": "中" * (MAX_TEXT_BYTES // 3 + 1)}]
        counts = consume_events(self.config, self.store, [self.event(**change) for change in changes])
        self.assertEqual(counts["invalid"], len(changes))
        self.assertEqual(counts["inserted"], 0)

    def test_message_and_observation_timestamps_are_required_independent_and_timezone_aware(self):
        """Verify message and observation timestamps are required independent and timezone aware."""
        bad = [self.event(message_timestamp="2026-11-07T12:00:00"), self.event(event_observed_at="yesterday"),
               self.event(message_timestamp=1234567890), self.event(message_timestamp=""),
               self.event(event_observed_at="2026-11-07T12:00:00")]
        counts = consume_events(self.config, self.store, bad)
        self.assertEqual(counts["invalid"], len(bad))
        local_time = self.now.astimezone(timezone(timedelta(hours=8))).isoformat()
        counts = consume_events(self.config, self.store, [self.event(message_timestamp=local_time)])
        self.assertEqual(counts["inserted"], 1)
        record = self.store.get_messages()["messages"][0]
        self.assertEqual(record["timestamp"], format_timestamp(self.now))

    def test_retention_uses_message_time_not_recent_observation(self):
        """Verify retention uses message time not recent observation."""
        counts = consume_events(self.config, self.store, [self.event(
            message_timestamp=format_timestamp(self.now - timedelta(days=31)),
            event_observed_at=format_timestamp(self.now))])
        self.assertEqual(counts["expired"], 1)
        self.assertEqual(counts["inserted"], 0)
        tighter = replace(self.config, retention_days=1)
        counts = consume_events(tighter, self.store, [self.event(message_timestamp=format_timestamp(self.now - timedelta(days=2)))])
        self.assertEqual(counts["expired"], 1)

    def test_null_message_time_uses_explicitly_labeled_observation_time(self):
        """Verify null message time uses explicitly labeled observation time."""
        event = self.event(message_timestamp=None)
        counts = consume_events(self.config, self.store, [json.dumps(event).encode("utf-8")])
        self.assertEqual(counts["inserted"], 1)
        record = self.store.get_messages()["messages"][0]
        self.assertEqual(record["timestamp"], event["event_observed_at"])
        self.assertEqual(record["source"], REAL_SOURCE)
        self.assertEqual(record["confidence"], 0)
        metadata = record["metadata"]
        self.assertEqual(metadata["timestamp_kind"], "event_observation_time")
        self.assertTrue(metadata["original_message_timestamp_unknown"])
        self.assertEqual(metadata["message_time_verification"], "unknown")
        self.assertEqual(metadata["structured_verification"], "pending")
        self.assertEqual(metadata["account_identity"], "unknown")
        self.assertFalse(metadata["native_callback_verified"])
        self.assertNotIn("native_created_at_raw", metadata)
        counts = consume_events(self.config, self.store, [self.event(message_id="expired-unknown", message_timestamp=None,
                                                                  event_observed_at=format_timestamp(self.now - timedelta(days=31)))])
        self.assertEqual(counts["expired"], 1)
        counts = consume_events(self.config, self.store, [self.event(message_timestamp=None, event_observed_at=None),
                                                        self.event(message_timestamp=None, native_created_at_raw="1794000000000")])
        self.assertEqual(counts["invalid"], 2)
        self.assertEqual(counts["inserted"], 0)

    def test_both_caller_and_store_conversation_policies_apply(self):
        """Verify both caller and store conversation policies apply."""
        caller_config = replace(self.config, allowed_conversations=("allowed", "excluded"), excluded_conversations=("excluded",))
        counts = consume_events(caller_config, self.store, [self.event(conversation_id="allowed"),
                                                          self.event(conversation_id="excluded"),
                                                          self.event(conversation_id="unlisted")])
        self.assertEqual((counts["inserted"], counts["policy_filtered"]), (1, 2))
        self.store.close()
        self.store = Store(self.config.database_path, replace(self.config, excluded_conversations=("store-excluded",)))
        counts = consume_events(self.config, self.store, [self.event(conversation_id="store-excluded")])
        self.assertEqual(counts["policy_filtered"], 1)

    def test_synthetic_factory_and_real_dtos_never_mix_destinations(self):
        """Verify synthetic factory and real dtos never mix destinations."""
        fixture = make_synthetic_event(text="PRIVATE SYNTHETIC BODY")
        self.assertNotIn("PRIVATE SYNTHETIC BODY", repr(fixture))
        counts = consume_events(self.config, self.store, [fixture])
        self.assertEqual(counts["destination_rejected"], 1)
        self.assertEqual(self.store.status()["record_count"], 0)
        self.store.mark_synthetic()
        counts = consume_events(self.config, self.store, [self.event(), fixture, fixture])
        self.assertEqual(counts["destination_rejected"], 1)
        self.assertEqual(counts["inserted"], 1)
        self.assertEqual(counts["duplicates_or_filtered"], 1)
        record = self.store.get_messages()["messages"][0]
        self.assertEqual(record["source"], "synthetic")
        self.assertTrue(record["metadata"]["synthetic"])
        self.assertEqual(record["metadata"]["provenance"], "synthetic_fixture")
        self.assertFalse(record["metadata"]["native_callback_verified"])
        self.assertTrue(record["metadata"]["original_message_timestamp_unknown"])
        self.assertEqual(record["metadata"]["timestamp_kind"], "event_observation_time")

    def test_malformed_duplicate_deep_and_oversized_frames_do_not_stop_following_data(self):
        """Verify malformed duplicate deep and oversized frames do not stop following data."""
        event = self.event()
        valid = json.dumps(event)
        duplicate = valid[:-1] + ',"text":"PRIVATE DUPLICATE BODY"}'
        frames = [b"\xff", "not json PRIVATE BODY", "[" * 10000 + "0" + "]" * 10000,
                  duplicate, b" " * (MAX_FRAME_BYTES + 1), self.event(text="x" * (MAX_TEXT_BYTES + 1)),
                  valid[:-1] + ',"text":NaN}', [], self.event()]
        counts = consume_events(self.config, self.store, frames)
        self.assertEqual(counts["received"], len(frames))
        self.assertEqual(counts["invalid"], len(frames) - 1)
        self.assertEqual(counts["inserted"], 1)

    def test_results_and_operational_errors_never_log_or_return_body(self):
        """Verify results and operational errors never log or return body."""
        private_text = "PRIVATE BODY MUST STAY IN STORE"
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            counts = consume_events(self.config, self.store, [self.event(text=private_text), "PRIVATE INVALID BODY"])
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        self.assertTrue(all(isinstance(value, int) for value in counts.values()))
        self.assertNotIn(private_text, json.dumps(counts))
        self.assertEqual(self.store.get_messages()["messages"][0]["text"], private_text)
        with patch.object(self.store, "ingest", side_effect=RuntimeError("PRIVATE STORAGE BODY")):
            counts = consume_events(self.config, self.store, [self.event(message_id="another-message")])
        self.assertEqual(counts["storage_errors"], 1)
        self.assertNotIn("PRIVATE STORAGE BODY", json.dumps(counts))

    def test_iterator_error_is_counted_without_raw_exception_and_configuration_mismatch_is_safe(self):
        """Verify iterator error is counted without raw exception and configuration mismatch is safe."""
        def events():
            """Fixture helper: events."""
            yield self.event()
            raise RuntimeError("PRIVATE ITERATOR BODY")

        counts = consume_events(self.config, self.store, events())
        self.assertEqual((counts["received"], counts["inserted"], counts["stream_errors"]), (1, 1, 1))
        self.assertNotIn("PRIVATE ITERATOR BODY", json.dumps(counts))
        wrong = replace(self.config, db_path=self.directory / "different.sqlite3")
        with self.assertRaisesRegex(NativeReceiveError, "native_receive:database_mismatch"):
            consume_events(wrong, self.store, [self.event()])


if __name__ == "__main__":
    unittest.main()
