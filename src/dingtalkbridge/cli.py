"""Local CLI. Imports only caller-selected files and never opens a network port."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta
import importlib
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any

from .config import Config, load_config
from .models import UTC, format_timestamp
from .store import Store


def _print(value: Any) -> None:
    """Print one UTF-8 JSON result for human-facing commands."""
    print(json.dumps(value, ensure_ascii=False, indent=2))


def parser() -> argparse.ArgumentParser:
    """Define archive, MCP, accessibility and native collection commands."""
    result = argparse.ArgumentParser(prog="dingtalkbridge", description="Local read-only DingTalk archive and MCP stdio bridge")
    result.add_argument("--config", type=Path, help="JSON settings path; overrides DINGTALKBRIDGE_CONFIG, then LOCALAPPDATA/DingTalkBridge/settings.json")
    result.add_argument("--db", type=Path, help="Override the bridge database path")
    result.add_argument("--demo-data", action="store_true", help="Use the separate synthetic demo database for status/serve")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Print local bridge status")
    commands.add_parser("serve", help="Run read-only MCP over stdio; stdout is protocol-only")
    importer = commands.add_parser("import-jsonl", help="Read a selected local normalized JSONL file into the bridge database")
    importer.add_argument("path", type=Path)
    commands.add_parser("demo", help="Create synthetic records only in the separate demo database")
    native = commands.add_parser("native", help="Exact-build native notification hook on the Default desktop")
    native.add_argument("--mode", choices=("probe", "count", "capture", "stop"), default="probe")
    native.add_argument("--pid", type=int)
    native.add_argument("--hwnd", type=lambda value: int(value, 0))
    native.add_argument("--duration", type=float, default=30, help="Seconds, 0 until Ctrl+C; maximum 3600")
    collector = commands.add_parser("collect", help="Collect authorized local Windows UI observations")
    collector.add_argument("--backend", choices=("uia", "ocr"), default="uia", help="Local collection backend (default: uia)")
    collector.add_argument("--mode", choices=("probe", "snapshot", "watch"), default=None,
                           help="Collection mode (default: uia=watch, ocr=snapshot)")
    collector.add_argument("--duration", type=float, default=0, help="Seconds; zero watches until interrupted")
    collector.add_argument("--pid", type=int)
    collector.add_argument("--interval", type=float, default=5)
    collector.add_argument("--include-screen-context", "--store-screen-context", dest="include_screen_context", action="store_true",
                           help="Explicitly archive visible screen context with provenance; it is not a structured message")
    return result


def import_jsonl(store: Store, path: Path) -> dict[str, Any]:
    """Import bounded caller-selected JSONL records and report counts only."""
    if store.dataset_kind == "synthetic":
        raise ValueError("imports cannot target the synthetic demo database")
    counts = {"lines": 0, "inserted": 0, "skipped": 0, "invalid": 0}
    # Limit each record to 1 MiB; error counts deliberately omit raw record text.
    with path.open("rb") as handle:
        while True:
            line = handle.readline(1024 * 1024 + 1)
            if not line:
                break
            counts["lines"] += 1
            if len(line) > 1024 * 1024:
                while line and not line.endswith(b"\n"):
                    line = handle.readline(1024 * 1024 + 1)
                counts["invalid"] += 1
                continue
            if not line.strip():
                counts["skipped"] += 1
                continue
            try:
                record = json.loads(line.decode("utf-8-sig"))
                added = store.ingest(record)
            except (ValueError, TypeError, RecursionError):
                counts["invalid"] += 1
            else:
                counts["inserted" if added else "skipped"] += 1
    return {"input_read_only": True, **counts}


def create_demo(store: Store) -> dict[str, Any]:
    """Populate the separate synthetic database with a few example messages."""
    store.mark_synthetic()
    now = datetime.now(UTC)
    inserted = 0
    samples = (
        ("demo-project", "Synthetic project", "Alice (synthetic)", "演示消息：今天讨论只读桥接测试。"),
        ("demo-project", "Synthetic project", "Bo (synthetic)", "Demo: search and context work entirely on synthetic records."),
        ("demo-project", "Synthetic project", "Alice (synthetic)", "演示消息：没有读取任何真实钉钉聊天。"),
        ("demo-notes", "Synthetic notes", "Demo bot (synthetic)", "Demo only: cursor polling returns newly archived records."),
    )
    for index, (conversation_id, title, sender, text) in enumerate(samples):
        inserted += store.ingest({"id": f"synthetic-demo-{index + 1}", "conversation_id": conversation_id,
                                  "conversation_title": title, "sender_id": f"synthetic-{index % 2}",
                                  "sender_name": sender, "text": text,
                                  "timestamp": format_timestamp(now - timedelta(minutes=len(samples) - index)),
                                  "source": "synthetic", "confidence": 1,
                                  "metadata": {"synthetic": True, "timestamp_kind": "synthetic_send_time"}})
    return {"synthetic": True, "inserted": inserted, **store.status()}


def main(argv: list[str] | None = None) -> int:
    """Dispatch CLI commands, keeping MCP stdout separate from diagnostics."""
    arguments = parser().parse_args(argv)
    if arguments.command == "collect" and arguments.mode is None:
        arguments.mode = "snapshot" if arguments.backend == "ocr" else "watch"
    # Python launched by an MCP host may otherwise inherit a Windows code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        config = load_config(arguments.config)
        if arguments.db is not None:
            config = replace(config, db_path=arguments.db)
        if arguments.command == "demo" or arguments.demo_data:
            if arguments.db is not None:
                raise ValueError("--db cannot be combined with demo or --demo-data")
            if arguments.demo_data and arguments.command not in {"status", "serve", "demo"}:
                raise ValueError("--demo-data is available only for status and serve")
            config = replace(config, db_path=config.data_dir / "demo" / "synthetic.sqlite3",
                             allowed_conversations=(), excluded_conversations=())
        if arguments.command == "collect":
            if not math.isfinite(arguments.duration) or not math.isfinite(arguments.interval) or arguments.duration < 0 or arguments.interval <= 0 or (arguments.pid is not None and arguments.pid <= 0):
                raise ValueError("duration must be nonnegative, interval and pid must be positive")
        with Store(config.database_path, config) as store:
            if arguments.command == "status":
                _print(store.status())
            elif arguments.command == "serve":
                from .mcp import serve
                serve(store)
            elif arguments.command == "import-jsonl":
                _print(import_jsonl(store, arguments.path))
            elif arguments.command == "demo":
                _print(create_demo(store))
            elif arguments.command == "native":
                from .native_target import discover_targets, select_target
                if arguments.mode == "probe":
                    _print({"targets": discover_targets(), "attached": False})
                elif arguments.mode == "stop":
                    from .native_controller import request_stop
                    _print(request_stop(config))
                else:
                    from .native_controller import run_collect
                    target = select_target(arguments.pid, arguments.hwnd)
                    arguments.pid, arguments.hwnd = target["pid"], target["hwnd"]
                    result = run_collect(config, store, arguments)
                    _print(result)
                    if result.get("status") == "failed":
                        return 1
            elif arguments.command == "collect":
                if store.dataset_kind == "synthetic":
                    raise ValueError("collection cannot target a synthetic demo database")
                collector = importlib.import_module(".ocr" if arguments.backend == "ocr" else ".collector", __package__)
                error_type = getattr(collector, "CollectorError", None) or getattr(collector, "OcrError", ValueError)
                try:
                    result = collector.run_collect(config, store, arguments)
                except error_type:
                    print("DingTalkBridge: local collector could not complete; check bridge status", file=sys.stderr)
                    return 1
                if arguments.backend == "ocr" and (not isinstance(result, dict) or result.get("status") != "available"):
                    if isinstance(result, dict):
                        _print(result)
                    print("DingTalkBridge: OCR collection did not produce an available observation; check bridge status", file=sys.stderr)
                    return 130 if isinstance(result, dict) and result.get("interrupted") is True else 1
                _print(result)
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, sqlite3.Error, ImportError) as exc:
        # Error classes are actionable without printing possibly sensitive values.
        if isinstance(exc, ValueError):
            message = str(exc)
        elif isinstance(exc, ImportError):
            message = "collector module is not available"
        else:
            message = f"local operation failed ({type(exc).__name__}); check selected paths and permissions"
        print("DingTalkBridge: " + message, file=sys.stderr)
        return 1
