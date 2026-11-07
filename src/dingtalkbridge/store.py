"""Private SQLite archive and bounded, policy-filtered read queries."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from .config import Config
from .models import UTC, format_timestamp, normalize_record, parse_timestamp, record_fingerprint


SCREEN_CONTEXT_SOURCES = (
    "windows_uia_screen", "windows_msaa_screen",
    "windows_accessibility_screen", "windows_ocr_screen",
)


class Store:
    def __init__(self, path: str | Path, config: Config | None = None) -> None:
        self.path = Path(path)
        self.config = config or Config(db_path=self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS bridge_metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, last_timestamp REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL UNIQUE,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                conversation_title TEXT NOT NULL,
                sender_id TEXT NOT NULL, sender_name TEXT NOT NULL, text TEXT NOT NULL,
                timestamp TEXT NOT NULL, timestamp_epoch REAL NOT NULL,
                source TEXT NOT NULL, confidence REAL NOT NULL, metadata TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_conversation_time
                ON messages(conversation_id,timestamp_epoch,seq);
            CREATE INDEX IF NOT EXISTS messages_time ON messages(timestamp_epoch,seq);
        """)
        self.fts_available = self._initialize_fts()
        self._last_prune: datetime | None = None

    def _initialize_fts(self) -> bool:
        existing = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='messages_fts'"
        ).fetchone()
        try:
            self.connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING "
                "fts5(text,content='messages',content_rowid='seq')"
            )
        except sqlite3.OperationalError as exc:
            if "no such module" in str(exc).lower():
                return False
            raise
        self.connection.executescript("""
            CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid,text) VALUES(new.seq,new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts,rowid,text) VALUES('delete',old.seq,old.text);
            END;
            CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts,rowid,text) VALUES('delete',old.seq,old.text);
                INSERT INTO messages_fts(rowid,text) VALUES(new.seq,new.text);
            END;
        """)
        if not existing:
            self.connection.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        self.connection.commit()
        return True

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _cutoff(self) -> float:
        return (datetime.now(UTC) - timedelta(days=self.config.retention_days)).timestamp()

    def prune(self) -> int:
        """Apply configured retention to this bridge's own database only."""
        with self.connection:
            removed = self.connection.execute(
                "DELETE FROM messages WHERE timestamp_epoch < ?", (self._cutoff(),)
            ).rowcount
            self.connection.execute(
                "DELETE FROM conversations WHERE NOT EXISTS "
                "(SELECT 1 FROM messages WHERE messages.conversation_id=conversations.id)"
            )
        return removed

    def ingest(self, record: Mapping[str, Any]) -> bool:
        normalized = normalize_record(record)
        if self.dataset_kind == "synthetic" and normalized["source"] != "synthetic":
            raise ValueError("synthetic demo databases cannot receive real records")
        now = datetime.now(UTC)
        if self._last_prune is None or (now - self._last_prune).total_seconds() >= 60:
            self.prune()
            self._last_prune = now
        epoch = parse_timestamp(normalized["timestamp"]).timestamp()
        if not self.config.permits(normalized["conversation_id"]) or epoch < self._cutoff():
            return False
        with self.connection:
            # Do not let a duplicate alter conversation metadata.
            if self.connection.execute(
                "SELECT 1 FROM messages WHERE id=? OR fingerprint=?",
                (normalized["id"], record_fingerprint(normalized)),
            ).fetchone():
                return False
            self.connection.execute(
                "INSERT INTO conversations(id,title,last_timestamp) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET title=excluded.title,last_timestamp=excluded.last_timestamp "
                "WHERE excluded.last_timestamp >= conversations.last_timestamp",
                (normalized["conversation_id"], normalized["conversation_title"], epoch),
            )
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO messages(id,fingerprint,conversation_id,conversation_title,"
                "sender_id,sender_name,text,timestamp,timestamp_epoch,source,confidence,metadata) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (normalized["id"], record_fingerprint(normalized), normalized["conversation_id"],
                 normalized["conversation_title"], normalized["sender_id"], normalized["sender_name"],
                 normalized["text"], normalized["timestamp"], epoch, normalized["source"],
                 normalized["confidence"], json.dumps(normalized["metadata"], ensure_ascii=False)),
            )
        return cursor.rowcount == 1

    def _limit(self, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= self.config.max_tool_results:
            raise ValueError(f"limit must be an integer between 1 and {self.config.max_tool_results}")
        return value

    @staticmethod
    def _cursor(value: str | None) -> int:
        if value is None:
            return 0
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 18:
            raise ValueError("cursor must be a nonnegative decimal string returned by the bridge")
        return int(value)

    @staticmethod
    def _conversation(value: str | None) -> None:
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("conversation_id must be a nonempty string")

    def _where(self, conversation_id: str | None = None) -> tuple[list[str], list[Any]]:
        self._conversation(conversation_id)
        clauses, parameters = ["m.timestamp_epoch >= ?"], [self._cutoff()]
        if conversation_id is not None:
            clauses.append("m.conversation_id=?")
            parameters.append(conversation_id)
        for field, operator in (("allowed_conversations", "IN"), ("excluded_conversations", "NOT IN")):
            ids = getattr(self.config, field)
            if ids:
                clauses.append(f"m.conversation_id {operator} ({','.join('?' for _ in ids)})")
                parameters.extend(ids)
        return clauses, parameters

    @staticmethod
    def _message(row: sqlite3.Row) -> dict[str, Any]:
        result = {key: row[key] for key in (
            "id", "conversation_id", "conversation_title", "sender_id", "sender_name",
            "text", "timestamp", "source", "confidence"
        )}
        result["metadata"] = json.loads(row["metadata"])
        return result

    def status(self) -> dict[str, Any]:
        """Report archive counts and the last collector heartbeat, not full coverage."""
        clauses, parameters = self._where()
        row = self.connection.execute(
            "SELECT COUNT(*) AS messages,COUNT(DISTINCT conversation_id) AS conversations,"
            "MIN(timestamp) AS first_timestamp,MAX(timestamp) AS last_timestamp,"
            "SUM(CASE WHEN source IN (" + ",".join("?" for _ in SCREEN_CONTEXT_SOURCES)
            + ") THEN 1 ELSE 0 END) AS screen_contexts "
            "FROM messages m WHERE " + " AND ".join(clauses), (*SCREEN_CONTEXT_SOURCES, *parameters),
        ).fetchone()
        collector = self._collector_status()
        return {
            "local_only": True,
            "dataset_kind": self.dataset_kind,
            "read_only_tools": True,
            "database_path": str(self.path.resolve()),
            "message_count": row["messages"] - (row["screen_contexts"] or 0),
            "record_count": row["messages"],
            "screen_context_count": row["screen_contexts"] or 0,
            "verified_message_count": 0,
            "conversation_count": row["conversations"],
            "first_timestamp": row["first_timestamp"],
            "last_timestamp": row["last_timestamp"],
            "retention_days": self.config.retention_days,
            "max_tool_results": self.config.max_tool_results,
            "allowed_conversations": list(self.config.allowed_conversations),
            "excluded_conversations": list(self.config.excluded_conversations),
            "full_text_search": self.fts_available,
            "collection_state": collector.get("status", "unknown"),
            "collector": collector,
            "coverage": "Only locally collected or explicitly imported records are available; no completeness guarantee. Windows UIA, MSAA, mixed accessibility and OCR screen records are screen context, not verified message history; navigation labels and input hints are not messages. Their timestamp is observation time, not message send time. message_count excludes these screen records; record_count includes them.",
        }

    def _collector_status(self) -> dict[str, Any]:
        """Expose bounded metadata only; a recent heartbeat is not an OS liveness check."""
        if self.dataset_kind == "synthetic":
            return {"status": "synthetic_demo", "verified_message_count": 0}
        path = self.config.data_dir / "collector-status.json"
        try:
            with path.open("rb") as handle:
                raw = handle.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                return {"status": "unavailable", "reason": "status_file_too_large"}
            result = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(result, dict):
                return {"status": "unavailable", "reason": "invalid_status_file"}
            expected_hash = hashlib.sha256(str(self.path.resolve()).casefold().encode("utf-8")).hexdigest()
            if result.get("database_path_hash") != expected_hash:
                return {"status": "not_applicable", "reason": "different_or_unidentified_database"}
            # This file is a last observation, not proof an OS process is alive.
            # Only explicit summary fields with bounded types are exposed; a
            # modified file must not become an arbitrary document/text resource.
            summary: dict[str, Any] = {"running": False, "process_liveness_verified": False,
                                       "verified_message_count": 0, "message_count": 0,
                                       "account_identity": "unknown", "history_available": False,
                                       "native_callback_verified": False}
            observation = result.get("status")
            summary["last_observation_status"] = observation if isinstance(observation, str) and observation in {
                "starting", "available", "no_visible_window", "process_not_running",
                "collector_error", "stopped", "unavailable", "unsupported", "isolated_desktop",
                "unsafe_geometry", "blank_capture", "capture_unavailable", "ocr_unavailable",
                "no_text", "target_changed", "dpi_unavailable", "completed", "rejected", "cancelled", "failed"
            } else "unknown"
            for key in ("snapshots", "scope_generation", "event_snapshots", "event_subscriptions",
                        "ingested_screen_contexts", "skipped_screen_contexts", "window_count",
                        "visited_nodes", "excluded_nodes", "provider_errors", "bridge_session_id",
                        "target_window_count", "hidden_window_count"):
                value = result.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**31 - 1:
                    summary[key] = value
            for key in ("captured_text_items", "screen_contexts_added"):
                value = result.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000:
                    summary[key] = value
            regions = result.get("evidence_region_counts")
            if isinstance(regions, dict):
                summary["evidence_region_counts"] = {
                    region: value for region in ("chat_component", "conversation_list", "other")
                    if isinstance(value := regions.get(region), int)
                    and not isinstance(value, bool) and 0 <= value <= 1_000_000
                }
            for key in ("screen_context_storage", "interrupted", "graceful_stop", "truncated",
                        "cropSafety", "post_capture_geometry_verified"):
                if isinstance(result.get(key), bool):
                    summary[key] = result[key]
            for key in ("last_observed_at", "heartbeat_at", "stopped_at"):
                if isinstance(result.get(key), str) and len(result[key]) <= 64:
                    try:
                        summary[key] = format_timestamp(parse_timestamp(result[key]))
                    except ValueError:
                        pass
            for key, valid in (("mode", {"probe", "snapshot", "watch", "user_confirmed_roi_once", "native_count", "native_capture"}),
                               ("source", (*SCREEN_CONTEXT_SOURCES, "windows_native_event_pending")),
                               ("scope", {"current_visible_windows"}),
                               ("coverage", {"visible_accessibility_text_only", "visible_chat_display_region_ocr_only",
                                             "cropped_visible_chat_component_only", "native_event_dto_pending"}),
                               ("desktop_kind", {"interactive_default", "isolated_sandbox", "other"})):
                if isinstance(result.get(key), str) and result[key] in valid:
                    summary[key] = result[key]
            for group, keys in {
                "native_counts": ("received", "inserted", "duplicates_or_filtered", "policy_filtered", "expired",
                                  "invalid", "destination_rejected", "storage_errors", "stream_errors"),
                "native_controller_counts": ("status", "callbacks_seen", "notifications_400", "messages_seen", "messages_sent",
                                             "dropped", "decode_errors", "pipe_failures", "identity_failures", "error"),
            }.items():
                counters = result.get(group)
                if isinstance(counters, dict):
                    summary[group] = {key: value for key in keys if isinstance(value := counters.get(key), int)
                                      and not isinstance(value, bool) and 0 <= value <= 2**63 - 1}
            session_id = result.get("collector_session_id")
            if isinstance(session_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", session_id):
                summary["collector_session_id"] = session_id
            desktop_name = result.get("desktop_name")
            if isinstance(desktop_name, str) and desktop_name and all(
                ord(character) >= 32 and ord(character) != 127 for character in desktop_name
            ):
                try:
                    if len(desktop_name.encode("utf-8")) <= 128:
                        summary["desktop_name"] = desktop_name
                except UnicodeError:
                    pass
            ids = result.get("process_ids")
            if isinstance(ids, list) and len(ids) <= 64 and all(
                isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid <= 2**32 - 1 for pid in ids
            ):
                summary["process_ids"] = ids
            interval = result.get("interval_seconds", 5)
            if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0 < interval <= 3600:
                interval = 5
            summary["interval_seconds"] = interval
            claimed_running = result.get("running", result.get("collector_running", False)) is True
            if claimed_running and not summary.get("stopped_at"):
                heartbeat = summary.get("heartbeat_at")
                age = (datetime.now(UTC) - parse_timestamp(heartbeat)).total_seconds() if heartbeat else None
                fresh = age is not None and -5 <= age <= max(15, 3 * interval)
                summary["running"] = fresh
                summary["status"] = "recent_heartbeat" if fresh else "stale"
                summary["heartbeat_age_seconds"] = round(age, 3) if age is not None else None
            else:
                summary["status"] = "stopped"
            return summary
        except FileNotFoundError:
            return {"status": "not_started"}
        except (OSError, ValueError, RecursionError):
            return {"status": "unavailable", "reason": "status_file_unreadable"}

    @property
    def dataset_kind(self) -> str:
        row = self.connection.execute("SELECT value FROM bridge_metadata WHERE key='dataset_kind'").fetchone()
        return row[0] if row else "local"

    def mark_synthetic(self) -> None:
        if self.dataset_kind != "synthetic" and self.connection.execute("SELECT 1 FROM messages LIMIT 1").fetchone():
            raise ValueError("demo requires an empty or existing synthetic database")
        with self.connection:
            self.connection.execute(
                "INSERT INTO bridge_metadata(key,value) VALUES('dataset_kind','synthetic') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )

    def list_conversations(self, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        limit, offset = self._limit(limit), self._cursor(cursor)
        clauses, parameters = self._where()
        rows = self.connection.execute(
            "SELECT c.id,c.title,COUNT(*) AS message_count,MAX(m.timestamp) AS last_timestamp "
            "FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE "
            + " AND ".join(clauses) + " GROUP BY c.id,c.title ORDER BY c.id LIMIT ? OFFSET ?",
            (*parameters, limit + 1, offset),
        ).fetchall()
        return {"conversations": [dict(row) for row in rows[:limit]],
                "next_cursor": str(offset + min(len(rows), limit)), "has_more": len(rows) > limit}

    def get_messages(self, conversation_id: str | None = None, limit: int = 50,
                     before: str | None = None, after: str | None = None) -> dict[str, Any]:
        limit = self._limit(limit)
        clauses, parameters = self._where(conversation_id)
        for value, operator in ((before, "<"), (after, ">")):
            if value is not None:
                clauses.append(f"m.timestamp_epoch {operator} ?")
                parameters.append(parse_timestamp(value).timestamp())
        rows = self.connection.execute(
            "SELECT m.* FROM messages m WHERE " + " AND ".join(clauses)
            + " ORDER BY m.timestamp_epoch DESC,m.seq DESC LIMIT ?", (*parameters, limit + 1),
        ).fetchall()
        return {"messages": [self._message(row) for row in reversed(rows[:limit])],
                "has_more": len(rows) > limit}

    def search_messages(self, query: str, conversation_id: str | None = None,
                        limit: int = 50) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 2048:
            raise ValueError("query must contain 1 to 2048 characters")
        limit = self._limit(limit)
        clauses, parameters = self._where(conversation_id)
        literal = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        if self.fts_available:
            # Quote user input as a literal phrase, never an FTS expression. LIKE
            # also finds Chinese substrings and punctuation-only searches.
            phrase = '"' + query.replace('"', '""') + '"'
            clauses.append("m.seq IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ? "
                           "UNION SELECT seq FROM messages WHERE text LIKE ? ESCAPE '\\')")
            parameters.extend((phrase, literal))
        else:
            clauses.append("m.text LIKE ? ESCAPE '\\'")
            parameters.append(literal)
        rows = self.connection.execute(
            "SELECT m.* FROM messages m WHERE " + " AND ".join(clauses)
            + " ORDER BY m.timestamp_epoch DESC,m.seq DESC LIMIT ?", (*parameters, limit + 1),
        ).fetchall()
        return {"messages": [self._message(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    def get_context(self, message_id: str, before: int = 10, after: int = 10) -> dict[str, Any]:
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id must be a nonempty string")
        for name, value in (("before", before), ("after", after)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if before + after + 1 > self.config.max_tool_results:
            raise ValueError("context size exceeds max_tool_results")
        clauses, parameters = self._where()
        anchor = self.connection.execute(
            "SELECT m.* FROM messages m WHERE m.id=? AND " + " AND ".join(clauses),
            (message_id, *parameters),
        ).fetchone()
        if anchor is None:
            return {"message": None, "messages": []}
        clauses, parameters = self._where(anchor["conversation_id"])
        neighbors = []
        for count, operator, direction in ((before, "<", "DESC"), (after, ">", "ASC")):
            rows = self.connection.execute(
                "SELECT m.* FROM messages m WHERE " + " AND ".join(clauses)
                + f" AND (m.timestamp_epoch,m.seq) {operator} (?,?) "
                + f"ORDER BY m.timestamp_epoch {direction},m.seq {direction} LIMIT ?",
                (*parameters, anchor["timestamp_epoch"], anchor["seq"], count),
            ).fetchall()
            neighbors.extend(rows)
        neighbors.append(anchor)
        neighbors.sort(key=lambda row: (row["timestamp_epoch"], row["seq"]))
        return {"message": self._message(anchor), "messages": [self._message(row) for row in neighbors]}

    def poll_events(self, cursor: str | None = None, limit: int = 50,
                    conversation_id: str | None = None) -> dict[str, Any]:
        limit, sequence = self._limit(limit), self._cursor(cursor)
        clauses, parameters = self._where(conversation_id)
        # Take a high-water mark before reading so a concurrent collector cannot
        # insert between the page and cursor queries and have its row skipped.
        maximum = self.connection.execute("SELECT COALESCE(MAX(seq),0) FROM messages").fetchone()[0]
        rows = self.connection.execute(
            "SELECT m.* FROM messages m WHERE m.seq>? AND m.seq<=? AND " + " AND ".join(clauses)
            + " ORDER BY m.seq LIMIT ?", (sequence, maximum, *parameters, limit + 1),
        ).fetchall()
        selected = rows[:limit]
        if len(rows) > limit:
            next_cursor = selected[-1]["seq"]
        else:
            next_cursor = max(sequence, maximum)
        return {"messages": [self._message(row) for row in selected],
                "next_cursor": str(next_cursor), "has_more": len(rows) > limit}
