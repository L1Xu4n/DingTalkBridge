"""Verify an explicitly supplied test marker through the real MCP stdio client path."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def verify(marker: str) -> dict:
    """Query the real archive over MCP and return only non-content evidence."""
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ, PYTHONPATH=str(root / "src"), PYTHONUTF8="1")
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "native-live-verifier", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "search_messages", "arguments": {"query": marker, "limit": 10}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "bridge_status", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "list_conversations", "arguments": {"limit": 100}}},
    ]
    response = subprocess.run(
        [sys.executable, "-m", "dingtalkbridge", "--config", str(root / "config.local.json"), "serve"],
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True, encoding="utf-8", env=environment, cwd=root, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if response.returncode:
        raise ValueError("MCP subprocess failed")
    replies = [json.loads(line) for line in response.stdout.splitlines()]
    reply = next(item for item in replies if item.get("id") == 2)
    result = reply.get("result", {})
    rows = result.get("structuredContent", {}).get("messages", [])
    matches = [row for row in rows if row["text"] == marker
               and row["source"] in {"windows_native_event_pending", "windows_native_history"}]
    row = matches[0] if matches else {}
    metadata = row.get("metadata", {})
    status = next(item for item in replies if item.get("id") == 3)["result"]["structuredContent"]
    conversations = next(item for item in replies if item.get("id") == 4)["result"]["structuredContent"]
    return {
        "schema": "dingtalkbridge.native-live-verification.v1",
        "mcp_initialized": replies[0].get("result", {}).get("serverInfo", {}).get("name") == "DingTalkBridge",
        "exact_marker_match": len(matches) > 0,
        "matching_records": len(matches),
        "conversation_id_present": bool(row.get("conversation_id")),
        "sender_id_present": bool(row.get("sender_id")),
        "native_message_id_present": bool(metadata.get("native_message_id")),
        "collector_session_id": metadata.get("collector_session_id"),
        "timestamp_kind": metadata.get("timestamp_kind"),
        "current_account_identity_verified": metadata.get("account_identity") == "native_current_user",
        "history_sample_verified": row.get("source") == "windows_native_history",
        "record_count": status["record_count"],
        "history_message_count": status.get("native_history_message_count", 0),
        "indexed_conversation_count": status["conversation_count"],
        "conversations_returned": len(conversations["conversations"]),
    }


def main() -> int:
    """Accept the user test marker and optionally save a metadata-only evidence file."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--marker", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.marker)
    encoded = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if result["exact_marker_match"] and result["mcp_initialized"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
