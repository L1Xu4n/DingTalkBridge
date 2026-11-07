"""Normalization shared by importers and local collectors.

Message bodies are data, never executable instructions. This module performs no I/O.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Mapping


UTC = timezone.utc
MAX_TEXT_BYTES = 128 * 1024
MAX_METADATA_BYTES = 32 * 1024


def parse_timestamp(value: Any) -> datetime:
    """Accept an ISO 8601 timestamp, datetime, or Unix seconds/milliseconds."""
    if isinstance(value, bool):
        raise ValueError("timestamp must be ISO 8601 or Unix seconds/milliseconds")
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("timestamp must be finite")
        seconds = value / 1000 if abs(value) >= 100_000_000_000 else value
        try:
            result = datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("timestamp is out of range") from exc
    elif isinstance(value, str) and value.strip():
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be ISO 8601") from exc
    else:
        raise ValueError("timestamp is required")
    if result.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return result.astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _string(record: Mapping[str, Any], key: str, default: str = "") -> str:
    value = record.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    maximum = MAX_TEXT_BYTES if key == "text" else 4096
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{key} exceeds the byte limit")
    return value


def normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise ValueError("message record must be an object")
    conversation_id = _string(record, "conversation_id").strip()
    if not conversation_id:
        raise ValueError("conversation_id is required")
    timestamp = format_timestamp(parse_timestamp(record.get("timestamp")))
    source = _string(record, "source", "import-jsonl").strip()
    if not source:
        raise ValueError("source must be nonempty")
    confidence = record.get("confidence", 1.0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be a number between 0 and 1")
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be a number between 0 and 1")
    metadata = record.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    try:
        # Reject NaN and non-JSON objects and detach mutable caller-owned data.
        serialized_metadata = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
        if len(serialized_metadata.encode("utf-8")) > MAX_METADATA_BYTES:
            raise ValueError("metadata exceeds the byte limit")
        metadata = json.loads(serialized_metadata)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("metadata must contain JSON values") from exc
    normalized = {
        "id": _string(record, "id"),
        "conversation_id": conversation_id,
        "conversation_title": _string(record, "conversation_title", conversation_id),
        "sender_id": _string(record, "sender_id"),
        "sender_name": _string(record, "sender_name"),
        "text": _string(record, "text"),
        "timestamp": timestamp,
        "source": source,
        "confidence": float(confidence),
        "metadata": metadata,
    }
    if not normalized["id"]:
        identity = [normalized[key] for key in (
            "source", "conversation_id", "sender_id", "sender_name", "timestamp", "text"
        )]
        normalized["id"] = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    return normalized


def record_fingerprint(record: Mapping[str, Any]) -> str:
    """Deduplicate repeated collection/import independently of a provided message ID."""
    identity = [record[key] for key in (
        "source", "conversation_id", "sender_id", "sender_name", "timestamp", "text"
    )]
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
