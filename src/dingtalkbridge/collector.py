"""Read-only Windows UIAutomation collection of currently visible DingTalk UI.

No UI text is classified as a chat message. Only summary evidence is written by
default; explicit opt-in can store a clearly labelled screen_context record.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
import math
import ntpath
import os
from pathlib import Path
import queue
import subprocess
import sysconfig
import threading
import time
from typing import Any, Iterator, Mapping
from uuid import uuid4


DEFAULT_PROCESS_PATH = r"D:\Program Files\DingDing\main\current_new\DingTalk.exe"
SCOPE = "current_visible_windows"
COVERAGE = "visible_accessibility_text_only"
MAX_CONTEXT_UTF8_BYTES = 128 * 1024


class CollectorError(RuntimeError):
    """An operational collector failure, with no captured UI text in its message."""


def _option(args: Any, name: str, default: Any) -> Any:
    if args is None:
        return default
    if isinstance(args, Mapping):
        return args.get(name, default)
    return getattr(args, name, default)


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise CollectorError(f"Invalid {name}.") from None
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise CollectorError(f"Invalid {name}.")
    return result


def ensure_helper(data_dir: Path) -> Path:
    """Use the bundled helper, or compile an isolated source-hashed local cache.

    Compilation does not need elevation, packages, network access, or a .NET SDK.
    The .NET Framework compiler and reference assemblies ship with Windows.
    """
    if os.name != "nt":
        raise CollectorError("The UIAutomation collector requires Windows.")
    project = Path(__file__).resolve().parents[2]
    source = project / "native" / "DingTalkUia.cs"
    if not source.is_file():
        source = Path(sysconfig.get_path("data")) / "share" / "dingtalkbridge" / "native" / "DingTalkUia.cs"
        if not source.is_file():
            raise CollectorError("The UIAutomation helper source is missing.")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:20]
    # Cache by source content so a stale bundled executable is never selected.
    output = Path(data_dir) / "native-cache" / f"DingTalkUia-{digest}.exe"
    if output.is_file():
        return output
    framework = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319"
    compiler = framework / "csc.exe"
    if not compiler.is_file():
        raise CollectorError("The .NET Framework 4.x C# compiler is unavailable.")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Compile to a unique temporary filename, then atomically install the cache.
    temporary = output.with_name(f".build-{uuid4().hex}.exe")
    command = [str(compiler), "/nologo", "/target:exe", "/optimize+", "/platform:x64", f"/out:{temporary}"]
    command.extend(f"/reference:{framework / 'WPF' / name}" for name in ("UIAutomationClient.dll", "UIAutomationTypes.dll", "WindowsBase.dll"))
    command.extend([f"/reference:{framework / 'System.Web.Extensions.dll'}", f"/reference:{framework / 'Accessibility.dll'}", str(source)])
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode != 0 or not temporary.is_file():
            raise CollectorError("The UIAutomation helper failed to compile.")
        os.replace(temporary, output)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectorError(f"The UIAutomation helper could not be built ({type(exc).__name__}).") from None
    finally:
        temporary.unlink(missing_ok=True)
    return output


def screen_context_records(snapshot: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Convert evidence into explicitly non-message records, without guessing IDs.

    The synthetic conversation ID identifies a visible native window, never a
    DingTalk group or contact. Timestamp is observation time, not send time.
    """
    if snapshot.get("type") != "snapshot" or snapshot.get("status") != "available":
        return
    observed_at = snapshot.get("observed_at")
    if not isinstance(observed_at, str) or not observed_at:
        return
    session_id = snapshot.get("collector_session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    scope_generation = snapshot.get("scope_generation", 0)
    for window in snapshot.get("evidence", []):
        if not isinstance(window, Mapping):
            continue
        pid, window_id = window.get("pid"), window.get("window_id")
        if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(window_id, str) or not window_id:
            continue
        texts: list[str] = []
        regions: dict[str, int] = {}
        providers: dict[str, int] = {}
        for item in window.get("texts", []):
            if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                text = item["text"].strip()
                if text:
                    texts.append(text)
                    region = item.get("region", "window")
                    if region not in {"window", "chat_component", "conversation_list", "window_toolbar"}:
                        region = "window"
                    regions[region] = regions.get(region, 0) + 1
                    provider = "msaa" if item.get("provider") == "msaa" else "uia"
                    providers[provider] = providers.get(provider, 0) + 1
        if not texts:
            continue
        text = "\n".join(texts)
        encoded = text.encode("utf-8")
        context_truncated = len(encoded) > MAX_CONTEXT_UTF8_BYTES
        if context_truncated:
            text = encoded[:MAX_CONTEXT_UTF8_BYTES].decode("utf-8", errors="ignore")
        conversation_id = f"uia:visible-window:{session_id}:{scope_generation}:{pid}:{window_id}"
        record_key = json.dumps([conversation_id, text], ensure_ascii=False, separators=(",", ":"))
        yield {
            "id": "uia-screen-" + hashlib.sha256(record_key.encode("utf-8")).hexdigest(),
            "conversation_id": conversation_id,
            "conversation_title": "当前可见钉钉窗口（会话身份未知）",
            "sender_id": "unknown",
            "sender_name": "未知（界面证据）",
            "text": text,
            "timestamp": observed_at,
            "source": "windows_msaa_screen" if set(providers) == {"msaa"} else ("windows_accessibility_screen" if "msaa" in providers else "windows_uia_screen"),
            "confidence": 0.0,
            "metadata": {
                "kind": "screen_context",
                "is_message": False,
                "timestamp_kind": "observation_time",
                "sender_identity": "unavailable",
                "account_identity": "unknown",
                "conversation_identity": "unavailable",
                "content_classification": "unverified_visible_text",
                "desktop_kind": snapshot.get("desktop_kind", "unknown") if snapshot.get("desktop_kind") in {"interactive_default", "isolated_sandbox", "other"} else "unknown",
                "collector_session_id": session_id,
                "scope_generation": scope_generation,
                "scope": SCOPE,
                "coverage": COVERAGE,
                "history_available": False,
                "message_count": 0,
                "pid": pid,
                "window_id": window_id,
                "evidence_region_counts": regions,
                "accessibility_providers": providers,
                "truncated": bool(snapshot.get("truncated", False)) or context_truncated,
            },
        }


def _write_status(data_dir: Path, result: Mapping[str, Any]) -> None:
    """Publish only summary metadata for bridge_status, never raw UI text."""
    destination = data_dir / "collector-status.json"
    temporary = data_dir / f".collector-status-{uuid4().hex}.json"
    try:
        temporary.write_text(json.dumps(dict(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _summary_evidence(event: Mapping[str, Any]) -> dict[str, Any]:
    """Allowlist metadata; raw UI text must only enter Store after opt-in."""
    allowed = ("schema_version", "type", "sequence", "observed_at", "source", "reason", "status", "scope", "coverage", "privacy_rule", "history_available", "message_count", "identity_resolution", "account_identity", "collector_session_id", "scope_generation", "visited_nodes", "excluded_nodes", "provider_errors", "truncated", "event_subscriptions", "event_notifications", "bridge_session_id", "desktop_name", "desktop_kind", "hidden_window_count", "target_window_count", "limits")
    summary = {key: event[key] for key in allowed if key in event}
    summary["process_ids"] = event.get("process_ids", [])[:128]
    summary["windows"] = [
        {key: window[key] for key in ("pid", "window_id", "visited_nodes", "msaa_visited_nodes", "msaa_excluded_nodes", "msaa_provider_errors", "safe_text_count", "evidence_region_counts", "excluded_nodes", "exclusion_reasons", "control_types", "framework_ids", "class_names", "truncated") if key in window}
        for window in event.get("windows", [])[:32] if isinstance(window, Mapping)
    ]
    summary["raw_text_persisted"] = False
    return summary


def _write_evidence(path: Path, event: Mapping[str, Any]) -> None:
    """A single bounded JSONL metadata row replaces the preceding observation."""
    encoded = json.dumps(_summary_evidence(event), ensure_ascii=False, separators=(",", ":")) + "\n"
    if len(encoded.encode("utf-8")) > 65536:
        encoded = json.dumps({"type": event.get("type"), "status": event.get("status"), "observed_at": event.get("observed_at"), "scope": SCOPE, "metadata_truncated": True, "raw_text_persisted": False}) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".evidence-{uuid4().hex}.jsonl")
    try:
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _helper_lines(process: subprocess.Popen[str], mode: str, duration: float, interval: float) -> Iterator[str]:
    """Keep the caller interruptible even if a UIA provider blocks the helper.

    A daemon reader owns the blocking Windows pipe. The supervisor never waits
    on a UIA call and bounds startup/inactivity/one-shot collection deadlines.
    """
    messages: queue.Queue[Any] = queue.Queue(maxsize=16)

    def reader() -> None:
        try:
            assert process.stdout is not None
            for line in process.stdout:
                messages.put(line)
        except Exception:
            messages.put(CollectorError("The UIAutomation helper output could not be read."))
        finally:
            messages.put(None)

    threading.Thread(target=reader, name="uia-output-reader", daemon=True).start()
    started = last_output = time.monotonic()
    inactivity_limit = max(20.0, interval + 15.0)
    while True:
        now = time.monotonic()
        if (mode != "watch" and now - started > 20) or (mode == "watch" and now - last_output > inactivity_limit) or (mode == "watch" and duration > 0 and now - started > duration + 20):
            raise CollectorError("The UIAutomation helper timed out; the accessibility provider may be blocked.")
        try:
            item = messages.get(timeout=0.2)
        except queue.Empty:
            continue
        if item is None:
            return
        if isinstance(item, Exception):
            raise item
        last_output = time.monotonic()
        yield item


def _stop_process(process: subprocess.Popen[str], stop_file: Path) -> bool:
    """Request cooperative unsubscription before bounded termination fallback."""
    if process.poll() is not None:
        return True
    stop_file.touch(exist_ok=True)
    try:
        process.wait(timeout=5)
        return True
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        return False


def _launch_helper(command: list[str], stop_file: Path, startup_timeout: float = 20) -> subprocess.Popen[str]:
    """Bound CreateProcess as well as provider calls; never leave a late child."""
    arrival: queue.Queue[Any] = queue.Queue(maxsize=1)
    abandoned = threading.Event()
    guard = threading.Lock()
    pending: list[subprocess.Popen[str]] = []

    def launch() -> None:
        try:
            created = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8-sig", errors="strict", bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        except OSError as exc:
            arrival.put(exc)
            return
        with guard:
            late = abandoned.is_set()
            if not late:
                pending.append(created)
                arrival.put(created)
        if late:
            try:
                _stop_process(created, stop_file)
            finally:
                stop_file.unlink(missing_ok=True)

    threading.Thread(target=launch, name="uia-process-launcher", daemon=True).start()
    claimed = False
    try:
        try:
            item = arrival.get(timeout=startup_timeout)
        except queue.Empty:
            raise CollectorError("The UIAutomation helper startup timed out or was blocked by Windows.") from None
        if isinstance(item, OSError):
            raise CollectorError(f"Windows could not start the UIAutomation helper ({type(item).__name__}).") from None
        with guard:
            pending.remove(item)
            claimed = True
        return item
    finally:
        with guard:
            cleanup = list(pending) if not claimed else []
            if not claimed:
                abandoned.set()
                pending.clear()
        for child in cleanup:
            _stop_process(child, stop_file)


def run_collect(config: Any, store: Any, args: Any = None) -> dict[str, Any]:
    """Run probe/snapshot/watch and return a safe, text-free result dictionary.

    ``config.data_dir`` is required. ``store.ingest(record)->bool`` is called
    only if ``args.include_screen_context`` (or ``store_screen_context``) is true.
    Args can be argparse.Namespace or a mapping. Defaults to a single probe when
    called without args; the CLI supplies its own watch default.
    """
    mode = _option(args, "mode", "probe")
    if mode not in {"probe", "snapshot", "watch"}:
        raise CollectorError("Mode must be probe, snapshot, or watch.")
    duration = _number(_option(args, "duration", 0), "duration", 0, 86400 * 365)
    interval = _number(_option(args, "interval", 5), "interval", 1, 3600)
    pid = _option(args, "pid", None)
    if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
        raise CollectorError("PID must be a positive integer.")
    data_dir = Path(config.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    process_path = str(_option(args, "process_path", DEFAULT_PROCESS_PATH))
    if ntpath.basename(process_path).lower() != "dingtalk.exe":
        raise CollectorError("The process path must name DingTalk.exe.")
    include_context = bool(_option(args, "include_screen_context", False) or _option(args, "store_screen_context", False))
    session_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid4().hex[:10]
    evidence_path = data_dir / "evidence" / "uia-latest.jsonl" if mode != "probe" else None
    stop_file = data_dir / f".uia-stop-{session_id}"
    database_path = Path(getattr(store, "path", getattr(config, "database_path", data_dir / "messages.sqlite3"))).resolve()
    result: dict[str, Any] = {
        "mode": mode, "status": "starting", "scope": SCOPE, "coverage": COVERAGE,
        "history_available": False, "message_count": 0, "snapshots": 0,
        "verified_message_count": 0, "account_identity": "unknown",
        "collector_session_id": session_id, "scope_generation": 0,
        "event_snapshots": 0, "event_subscriptions": 0, "ingested_screen_contexts": 0,
        "event_notifications": 0,
        "skipped_screen_contexts": 0, "screen_context_storage": include_context,
        "evidence_file": str(evidence_path) if evidence_path else None,
        "interrupted": False, "graceful_stop": True, "collector_running": False, "running": False,
        "interval_seconds": interval, "heartbeat_at": _stamp(),
        "desktop_kind": "unknown",
        "database_path_hash": hashlib.sha256(str(database_path).casefold().encode("utf-8")).hexdigest(),
        "limitation": "Only currently visible non-editable accessibility text is observed. No chat-message, sender, group identity, send time, or historical completeness is inferred.",
    }
    process = None
    previous_scope = None
    try:
        _write_status(data_dir, result)
        helper = ensure_helper(data_dir)
        command = [str(helper), "--mode", mode, "--process-path", process_path, "--interval", str(interval), "--duration", str(duration), "--stop-file", str(stop_file)]
        if pid is not None:
            command.extend(["--pid", str(pid)])
        if _option(args, "diagnostics", False):
            command.extend(["--diagnostics", "1"])
        process = _launch_helper(command, stop_file)
        result["collector_running"] = True
        result["running"] = True
        result["heartbeat_at"] = _stamp()
        _write_status(data_dir, result)
        for line in _helper_lines(process, mode, duration, interval):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                raise CollectorError("The UIAutomation helper returned invalid JSON.") from None
            if not isinstance(event, dict) or event.get("type") not in {"probe", "snapshot", "status", "error"}:
                raise CollectorError("The UIAutomation helper returned an invalid event.")
            if event.get("type") == "error":
                raise CollectorError("The UIAutomation helper reported an operational error.")
            if event.get("type") in {"probe", "snapshot"}:
                scope_key = (tuple(sorted(event.get("process_ids", []))), tuple(sorted((window.get("pid"), window.get("window_id")) for window in event.get("windows", []))))
                if previous_scope is not None and scope_key != previous_scope:
                    result["scope_generation"] += 1
                previous_scope = scope_key
            event["collector_session_id"] = session_id
            event["scope_generation"] = result["scope_generation"]
            event["account_identity"] = "unknown"
            result["heartbeat_at"] = _stamp()
            if evidence_path is not None and event.get("type") in {"probe", "snapshot"}:
                _write_evidence(evidence_path, event)
            if event.get("type") in {"probe", "snapshot"}:
                result["status"] = event.get("status", "unknown")
                result["process_ids"] = event.get("process_ids", [])
                result["window_count"] = len(event.get("windows", []))
                result["visited_nodes"] = event.get("visited_nodes", 0)
                result["captured_text_items"] = sum(window.get("safe_text_count", 0) for window in event.get("windows", []))
                result["msaa_visited_nodes"] = sum(window.get("msaa_visited_nodes", 0) for window in event.get("windows", []))
                result["evidence_region_counts"] = {}
                result["exclusion_reasons"] = {}
                for window in event.get("windows", []):
                    for key in ("evidence_region_counts", "exclusion_reasons"):
                        for label, count in window.get(key, {}).items():
                            if key == "evidence_region_counts" and label not in {"chat_component", "conversation_list"}:
                                label = "other"
                            result[key][label] = result[key].get(label, 0) + count
                result["excluded_nodes"] = event.get("excluded_nodes", 0)
                result["provider_errors"] = event.get("provider_errors", 0)
                result["truncated"] = bool(event.get("truncated", False))
                result["event_subscriptions"] = max(result["event_subscriptions"], event.get("event_subscriptions", 0))
                result["event_notifications"] = event.get("event_notifications", 0)
                for key in ("bridge_session_id", "desktop_name", "desktop_kind", "hidden_window_count", "target_window_count"):
                    if key in event:
                        result[key] = event[key]
                if result.get("desktop_kind") == "isolated_sandbox":
                    result["status"] = "isolated_desktop"
                    result["limitation"] = "The collector is on an isolated sandbox desktop, not the user's interactive desktop. Zero visible windows here does not mean DingTalk has no user window."
                result["last_observed_at"] = event.get("observed_at")
            if event.get("type") == "snapshot":
                result["snapshots"] += 1
                if event.get("reason") == "uia_event":
                    result["event_snapshots"] += 1
                if include_context:
                    for record in screen_context_records(event):
                        try:
                            added = store.ingest(record)
                        except ValueError:
                            raise CollectorError("A screen context record was rejected by the storage schema.") from None
                        if added:
                            result["ingested_screen_contexts"] += 1
                        else:
                            result["skipped_screen_contexts"] += 1
            _write_status(data_dir, result)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            raise CollectorError("The UIAutomation helper did not exit after closing output.") from None
        if process.returncode != 0:
            raise CollectorError(f"The UIAutomation helper exited with code {process.returncode}.")
    except KeyboardInterrupt:
        result["interrupted"] = True
    except CollectorError:
        result["status"] = "collector_error"
        raise
    except OSError as exc:
        result["status"] = "collector_error"
        raise CollectorError(f"The UIAutomation collector could not run ({type(exc).__name__}).") from None
    finally:
        if process is not None:
            result["graceful_stop"] = _stop_process(process, stop_file)
            if process.stdout is not None:
                process.stdout.close()
            result["helper_exit_code"] = process.returncode
        stop_file.unlink(missing_ok=True)
        result["collector_running"] = False
        result["running"] = False
        result["stopped_at"] = _stamp()
        _write_status(data_dir, result)
    return result
