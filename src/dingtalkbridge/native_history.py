"""Normalize all-type messages returned by the client's native history service."""

from datetime import datetime
import re
from typing import Any

from .config import Config
from .models import UTC, MAX_TEXT_BYTES, format_timestamp, parse_timestamp
from .native_receive import native_record_id
from .store import Store

SCHEMA = "dingtalkbridge.native-history.v1"


def normalize_history(frame: dict[str, Any], session: str) -> dict[str, Any]:
    """Validate the private frame and retain message type plus the client's original content."""
    if set(frame) != {"schema", "collector_session_id", "account_id", "conversation_title", "fields"} or frame["schema"] != SCHEMA:
        raise ValueError("invalid native history frame")
    if frame["collector_session_id"] != session or not isinstance(frame["account_id"], str) or not re.fullmatch(r"[1-9][0-9]{0,18}", frame["account_id"]):
        raise ValueError("invalid history identity")
    fields = frame["fields"]
    if not isinstance(fields, dict) or len(fields) > 64 or any(not isinstance(key, str) or not isinstance(value, str) for key, value in fields.items()):
        raise ValueError("invalid history fields")
    for key, value in fields.items():
        if len(key.encode("utf-8")) > 128 or len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("history field too large")
    for key in ("cid", "mid"):
        value = fields.get(key, "")
        if not value or len(value.encode("utf-8")) > 4096 or value != value.strip() or any(ord(ch) < 32 for ch in value):
            raise ValueError("invalid history message ID")
    content_type = fields.get("content_type", "unknown")
    content = fields.get("content", "")
    raw_time = fields.get("create_time", "")
    original_time = None
    if re.fullmatch(r"[1-9][0-9]{0,15}", raw_time):
        try:
            original_time = format_timestamp(parse_timestamp(int(raw_time)))
        except ValueError:
            pass
    retained, used, truncated = {}, 0, False
    for key, value in fields.items():
        if key == "content":
            continue
        size = len(value.encode("utf-8"))
        if size > 8192 or used + size > 16 * 1024:
            truncated = True
            continue
        retained[key] = value
        used += size
    return {
        "id": native_record_id(fields["cid"], fields["mid"]),
        "conversation_id": fields["cid"], "conversation_title": frame["conversation_title"],
        "sender_id": fields.get("sender_uid", ""), "sender_name": "",
        "text": content if content else "[消息类型 " + content_type + "]",
        "timestamp": original_time or format_timestamp(datetime.now(UTC)),
        "source": "windows_native_history", "confidence": 1.0,
        "metadata": {
            "schema": SCHEMA, "kind": "message", "native_message_id": fields["mid"],
            "account_id": frame["account_id"], "account_identity": "native_current_user",
            "collector_session_id": session, "content_type": content_type,
            "client_fields": retained, "metadata_truncated": truncated,
            "structured_verification": "native_service", "native_callback_verified": True,
            "timestamp_kind": "client_create_time" if original_time else "event_observation_time",
            "original_message_timestamp_unknown": original_time is None,
            "content_representation": "client_text_or_serialized_content",
        },
    }


def consume_history(config: Config, store: Store, frame: dict[str, Any], session: str) -> dict[str, int]:
    """Apply archive policy and return counts only; account mismatches stop the caller."""
    if config.database_path.resolve() != store.path.resolve():
        raise ValueError("history database mismatch")
    record = normalize_history(frame, session)
    store.bind_native_account(frame["account_id"])
    counts = {key: 0 for key in ("received", "inserted", "updated", "duplicates_or_filtered", "policy_filtered", "expired",
                                "invalid", "destination_rejected", "storage_errors", "stream_errors")}
    counts["received"] = 1
    if not config.permits(record["conversation_id"]):
        counts["policy_filtered"] = 1
        return counts
    outcome = store.ingest_native_history(record)
    counts[outcome if outcome in {"inserted", "updated"} else "duplicates_or_filtered"] = 1
    return counts
