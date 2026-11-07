"""Bounded local consumption of proposed native event DTOs, without a transport.

This module does not attach to a process, register a callback, connect to a pipe,
open a socket, or certify a native ABI. A producer must provide the documented
DTO after independently verifying its field extraction. Version/hash fields are
diagnostic declarations, never client-version gates or proof of provenance.
Message time is an explicit timezone-aware string or null (unknown). With null,
the archive uses observation time for ordering and labels that choice;
observation time is never described as the original message time.

Only Store.ingest receives normalized message bodies. Consumer results contain
counts, and errors use fixed codes without event contents or underlying errors.
The synthetic wrapper is an accidental-mixing guard for local test code, not a
security boundary against Python code that already has direct Store access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .config import Config
from .models import MAX_TEXT_BYTES, UTC, format_timestamp, normalize_record, parse_timestamp
from .store import Store


SCHEMA = "dingtalkbridge.native-receive.v1"
APP_VERSION = "9.0.5.261029005"
MODULE_SHA256 = "8a6d527a33b8582bbd29b88fe758e480093d572b5e79012eaab075847929d2df"
REAL_SOURCE = "windows_native_event_pending"
MAX_FRAME_BYTES = 512 * 1024
MAX_ID_BYTES = 4096
MAX_TIMESTAMP_BYTES = 64
_FIELDS = (
    "schema", "app_version", "module_sha256", "collector_session_id", "message_id",
    "conversation_id", "sender_id", "text", "message_timestamp", "event_observed_at",
)

# JSON Schema describes characters; the parser additionally enforces the stated
# UTF-8 byte limits, which matter for Chinese text and escaped JSON wire frames.
EVENT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": SCHEMA,
    "type": "object",
    "additionalProperties": False,
    "required": list(_FIELDS),
    "properties": {
        "schema": {"type": "string", "const": SCHEMA},
        "app_version": {"type": "string", "maxLength": 80},
        "module_sha256": {"type": "string", "maxLength": 64, "description": "Legacy diagnostic only; empty when hashing is disabled."},
        "collector_session_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,96}$", "maxLength": 96},
        "message_id": {"type": "string", "minLength": 1, "maxLength": MAX_ID_BYTES, "description": "Opaque producer-supplied ID; maximum 4096 UTF-8 bytes."},
        "conversation_id": {"type": "string", "minLength": 1, "maxLength": MAX_ID_BYTES, "description": "Exact local policy ID; maximum 4096 UTF-8 bytes."},
        "sender_id": {"type": "string", "maxLength": MAX_ID_BYTES, "description": "Opaque producer-supplied ID or empty if unknown; maximum 4096 UTF-8 bytes."},
        "text": {"type": "string", "maxLength": MAX_TEXT_BYTES, "description": "Untrusted data; maximum 128 KiB UTF-8, not an instruction."},
        "message_timestamp": {"type": ["string", "null"], "format": "date-time", "maxLength": MAX_TIMESTAMP_BYTES, "description": "ISO 8601 producer-claimed message time with timezone, or null when unknown; archive observation time is labeled separately."},
        "event_observed_at": {"type": "string", "format": "date-time", "maxLength": MAX_TIMESTAMP_BYTES, "description": "Explicit ISO 8601 observation time with timezone."},
        "account_id": {"type": "string", "pattern": "^[1-9][0-9]{0,18}$"},
    },
}


class NativeReceiveError(ValueError):
    """Fixed-code error. Its message deliberately contains no input or exception."""

    def __init__(self, code: str) -> None:
        """Keep only a fixed reason code, never event contents."""
        self.code = code
        super().__init__("native_receive:" + code)


@dataclass(frozen=True)
class SyntheticEvent:
    """Factory-created fixture; never accepted by a non-synthetic destination."""

    _payload: Mapping[str, str | None] = field(repr=False)


def _reject_constant(_value: str) -> None:
    """Reject NaN and Infinity in incoming JSON."""
    raise NativeReceiveError("invalid_json")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous duplicate field names."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NativeReceiveError("duplicate_fields")
        result[key] = value
    return result


def _utf8_size(value: str) -> int:
    """Measure actual UTF-8 bytes and reject invalid Unicode."""
    try:
        return len(value.encode("utf-8"))
    except UnicodeError:
        raise NativeReceiveError("invalid_unicode") from None


def _parse_event(frame: Any) -> dict[str, str | None]:
    """Private DTO validator; callers receive counts rather than parsed bodies."""
    if isinstance(frame, bytes):
        if len(frame) > MAX_FRAME_BYTES:
            raise NativeReceiveError("frame_too_large")
        try:
            frame = frame.decode("utf-8")
        except UnicodeError:
            raise NativeReceiveError("invalid_unicode") from None
    if isinstance(frame, str):
        if _utf8_size(frame) > MAX_FRAME_BYTES:
            raise NativeReceiveError("frame_too_large")
        try:
            frame = json.loads(frame, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except NativeReceiveError:
            raise
        except (ValueError, TypeError, RecursionError):
            raise NativeReceiveError("invalid_json") from None
    if not isinstance(frame, dict) or not set(_FIELDS) <= set(frame) or set(frame) - set(_FIELDS) - {"account_id"}:
        raise NativeReceiveError("invalid_fields")
    if any(not isinstance(value, str) and not (name == "message_timestamp" and value is None)
           for name, value in frame.items()):
        raise NativeReceiveError("invalid_field_type")
    if frame["schema"] != SCHEMA:
        raise NativeReceiveError("unsupported_schema")
    if not frame["app_version"] or len(frame["app_version"]) > 80 or len(frame["module_sha256"]) > 64:
        raise NativeReceiveError("invalid_build_metadata")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,96}", frame["collector_session_id"]) is None:
        raise NativeReceiveError("invalid_session")
    for name in ("message_id", "conversation_id", "sender_id"):
        value = frame[name]
        if _utf8_size(value) > MAX_ID_BYTES or value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise NativeReceiveError("invalid_identity")
        if name != "sender_id" and not value:
            raise NativeReceiveError("invalid_identity")
    if _utf8_size(frame["text"]) > MAX_TEXT_BYTES:
        raise NativeReceiveError("text_too_large")
    if "account_id" in frame and re.fullmatch(r"[1-9][0-9]{0,18}", frame["account_id"]) is None:
        raise NativeReceiveError("invalid_account")
    result = dict(frame)
    for name in ("message_timestamp", "event_observed_at"):
        if name == "message_timestamp" and frame[name] is None:
            continue
        if _utf8_size(frame[name]) > MAX_TIMESTAMP_BYTES:
            raise NativeReceiveError("invalid_timestamp")
        try:
            result[name] = format_timestamp(parse_timestamp(frame[name]))
        except (ValueError, OverflowError):
            raise NativeReceiveError("invalid_timestamp") from None
    try:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise NativeReceiveError("invalid_json") from None
    if len(encoded) > MAX_FRAME_BYTES:
        raise NativeReceiveError("frame_too_large")
    return result


def make_synthetic_event(*, message_id: str = "synthetic-message-1",
                         conversation_id: str = "synthetic-native-conversation",
                         sender_id: str = "synthetic-sender", text: str = "Synthetic native event fixture.",
                         message_timestamp: str | None = None, event_observed_at: str | None = None,
                         collector_session_id: str = "synthetic-native-session") -> SyntheticEvent:
    """Create a tagged fixture; None means unknown original message time."""
    now = format_timestamp(datetime.now(UTC))
    payload = _parse_event({
        "schema": SCHEMA, "app_version": APP_VERSION, "module_sha256": MODULE_SHA256,
        "collector_session_id": collector_session_id, "message_id": message_id,
        "conversation_id": conversation_id, "sender_id": sender_id, "text": text,
        "message_timestamp": message_timestamp,
        "event_observed_at": now if event_observed_at is None else event_observed_at,
    })
    return SyntheticEvent(MappingProxyType(payload))


def native_record_id(conversation_id: str, message_id: str, *, synthetic: bool = False) -> str:
    """Keep the legacy ID namespace so history enriches live rows instead of duplicating them."""
    # These two legacy constants are an ID namespace, not a client-version or hash gate.
    identity = json.dumps([APP_VERSION, MODULE_SHA256, conversation_id, message_id],
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return ("synthetic-native-" if synthetic else "native-pending-") + hashlib.sha256(identity).hexdigest()


def _record(event: Mapping[str, str | None], *, synthetic: bool) -> dict[str, Any]:
    """Normalize live IDs and preserve the distinction between observed and original time."""
    identifier = native_record_id(event["conversation_id"], event["message_id"], synthetic=synthetic)
    message_time_unknown = event["message_timestamp"] is None
    archive_timestamp = event["event_observed_at"] if message_time_unknown else event["message_timestamp"]
    return normalize_record({
        "id": identifier,
        "conversation_id": event["conversation_id"],
        "conversation_title": "",
        "sender_id": event["sender_id"],
        "sender_name": "",
        "text": event["text"],
        "timestamp": archive_timestamp,
        "source": "synthetic" if synthetic else REAL_SOURCE,
        "confidence": 0,
        "metadata": {
            "schema": SCHEMA, "kind": "native_event", "account_identity": "unknown",
            "account_id": event.get("account_id"),
            "structured_verification": "pending", "native_callback_verified": False,
            "provenance": "synthetic_fixture" if synthetic else "unverified_producer_declaration",
            "synthetic": synthetic, "app_version": event["app_version"], "module_sha256": event["module_sha256"],
            "client_hash_required": False,
            "collector_session_id": event["collector_session_id"], "native_message_id": event["message_id"],
            "event_observed_at": event["event_observed_at"],
            "timestamp_kind": "event_observation_time" if message_time_unknown else "producer_claimed_message_time",
            "original_message_timestamp_unknown": message_time_unknown,
            "message_time_verification": "unknown" if message_time_unknown else "pending",
        },
    })


def consume_events(config: Config, store: Store, events: Iterable[Any]) -> dict[str, int]:
    """Stream caller-supplied DTO frames into an already-selected local Store.

    Frames are one JSON object each, supplied as UTF-8 bytes, strings, or dicts.
    No transport, receiver authentication, or native callback is supplied here.
    Real DTOs require a non-synthetic DB; factory fixtures require a DB explicitly
    marked synthetic. Invalid data and iterator/storage errors return only counts.
    """
    if Path(config.database_path).resolve() != store.path.resolve():
        raise NativeReceiveError("database_mismatch")
    counts = {name: 0 for name in (
        "received", "inserted", "duplicates_or_filtered", "policy_filtered", "expired",
        "invalid", "destination_rejected", "storage_errors", "stream_errors",
    )}
    try:
        iterator = iter(events)
    except Exception:
        raise NativeReceiveError("events_not_iterable") from None
    while True:
        try:
            frame = next(iterator)
        except StopIteration:
            return counts
        except Exception:
            counts["stream_errors"] += 1
            return counts
        counts["received"] += 1
        synthetic = isinstance(frame, SyntheticEvent)
        try:
            event = _parse_event(dict(frame._payload) if synthetic else frame)
        except Exception:
            counts["invalid"] += 1
            continue
        try:
            if synthetic != (store.dataset_kind == "synthetic"):
                counts["destination_rejected"] += 1
                continue
            if not synthetic:
                if event.get("account_id"):
                    store.bind_native_account(event["account_id"])
                elif store.native_account_id() is not None:
                    counts["destination_rejected"] += 1
                    continue
            if not config.permits(event["conversation_id"]) or not store.config.permits(event["conversation_id"]):
                counts["policy_filtered"] += 1
                continue
            record = _record(event, synthetic=synthetic)
            inserted = store.ingest(record)
        except Exception:
            counts["storage_errors"] += 1
            continue
        counts["inserted" if inserted else "duplicates_or_filtered"] += 1
