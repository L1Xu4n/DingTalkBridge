"""Start one shared, hidden collector when an MCP server opens."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Iterator

from .config import Config
from .native_controller import (
    CollectorError, _acquire_lock, _stamp, _validate_build, _write_status,
    default_controller_path, run_collect,
)
from .native_target import select_target
from .store import Store

IS_WINDOWS = os.name == "nt"
MAX_SETTINGS_BYTES = 64 * 1024


def _locked(config: Config, name: str) -> bool:
    """Probe an OS lock; a leftover lock file alone does not mean a process is alive."""
    try:
        handle = _acquire_lock(config, name)
    except CollectorError as error:
        if str(error) == "native_controller:collector_already_running":
            return True
        raise
    handle.close()
    return False


def _settings(config: Config) -> bytes:
    """Pass resolved effective settings privately, including database overrides and filters."""
    values = {
        "data_dir": str(config.data_dir.resolve()),
        "db_path": str(config.database_path.resolve()),
        "allowed_conversations": list(config.allowed_conversations),
        "excluded_conversations": list(config.excluded_conversations),
        "max_tool_results": config.max_tool_results,
        "history_interval_seconds": config.history_interval_seconds,
    }
    payload = json.dumps(values, ensure_ascii=True).encode("utf-8")
    if len(payload) > MAX_SETTINGS_BYTES:
        raise ValueError("collector settings exceed 64 KiB")
    return payload


def ensure_collector(config: Config, *, respect_stop_request: bool = False) -> dict[str, Any]:
    """Reuse a locked collector or launch a hidden worker without touching MCP stdout."""
    if not IS_WINDOWS:
        return {"status": "unsupported_platform"}
    if _locked(config, "native-autostart.lock") or _locked(config, "native-collector.lock"):
        return {"status": "already_running"}
    stop_path = config.data_dir / "native-stop.request"
    if respect_stop_request and stop_path.exists():
        return {"status": "stopped"}
    _validate_build(default_controller_path(), synthetic=False)
    payload = _settings(config)
    if not respect_stop_request:
        stop_path.unlink(missing_ok=True)
    source = Path(__file__).resolve().parents[1]
    environment = dict(os.environ, PYTHONPATH=str(source), PYTHONUTF8="1")
    startup = subprocess.STARTUPINFO()
    startup.lpDesktop = "winsta0\\default"
    process = subprocess.Popen(
        [sys.executable, "-m", "dingtalkbridge.autostart"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(source), env=environment, startupinfo=startup,
        creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True,
    )
    try:
        if process.stdin is None:
            raise OSError("collector input unavailable")
        process.stdin.write(payload)
        process.stdin.close()
    except OSError:
        process.terminate()
        process.wait(timeout=5)
        raise OSError("collector startup failed") from None
    return {"status": "starting", "pid": process.pid}


@contextmanager
def maintain_collector(config: Config, *, enabled: bool = True) -> Iterator[None]:
    """Keep MCP's shared worker available while respecting explicit stop requests."""
    if not enabled:
        yield
        return
    stopped = threading.Event()

    def ensure(*, respect_stop_request: bool = False) -> None:
        """Attempt recovery with fixed diagnostics and no private data on MCP stdout."""
        try:
            ensure_collector(config, respect_stop_request=respect_stop_request)
        except (OSError, ValueError):
            print("DingTalkBridge: collector unavailable; check native build and bridge_status.", file=sys.stderr)

    def watch() -> None:
        """Recheck worker ownership during idle periods without undoing a manual stop."""
        while not stopped.wait(5):
            if not (config.data_dir / "native-stop.request").exists():
                ensure(respect_stop_request=True)

    ensure()
    worker = threading.Thread(target=watch, name="native-collector-watch", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=1)


def run_worker(config: Config) -> int:
    """Wait and recover across client exits, requiring a new client after native rejection."""
    try:
        ownership = _acquire_lock(config, "native-autostart.lock")
    except CollectorError as error:
        if str(error) == "native_controller:collector_already_running":
            return 0
        raise
    try:
        if _locked(config, "native-collector.lock"):
            return 0
        _validate_build(default_controller_path(), synthetic=False)
        stop_path = config.data_dir / "native-stop.request"
        with Store(config.database_path, config) as store:
            if store.dataset_kind == "synthetic":
                return 0
            blocked_pid = None
            while not stop_path.exists():
                try:
                    target = select_target()
                except ValueError:
                    _write_status(config, {
                        "status": "waiting_for_client", "mode": "native_capture",
                        "running": True, "heartbeat_at": _stamp(), "interval_seconds": 2,
                        "database_path_hash": hashlib.sha256(str(store.path.resolve()).casefold().encode("utf-8")).hexdigest(),
                    })
                    time.sleep(2)
                    continue
                if stop_path.exists():
                    break
                if target["pid"] == blocked_pid:
                    time.sleep(2)
                    continue
                try:
                    result = run_collect(config, store, {**target, "mode": "capture", "duration": 0,
                                                        "preserve_stop_request": True})
                except CollectorError as error:
                    if str(error) == "native_controller:collector_already_running":
                        return 0
                    raise
                if result.get("interrupted"):
                    return 0
                if result.get("requires_client_restart"):
                    blocked_pid = target["pid"]
                    _write_status(config, {**result, "recovery_state": "waiting_for_client_restart"})
                if not stop_path.exists():
                    time.sleep(2)
            _write_status(config, {
                "status": "stopped", "mode": "native_capture", "running": False,
                "heartbeat_at": _stamp(), "stopped_at": _stamp(),
                "database_path_hash": hashlib.sha256(str(store.path.resolve()).casefold().encode("utf-8")).hexdigest(),
            })
            return 0
    finally:
        ownership.close()


def main() -> int:
    """Read the parent's bounded configuration from stdin and run the private worker."""
    raw = sys.stdin.buffer.read(MAX_SETTINGS_BYTES + 1)
    if len(raw) > MAX_SETTINGS_BYTES:
        return 1
    try:
        values = json.loads(raw.decode("utf-8"))
        if not isinstance(values, dict):
            return 1
        return run_worker(Config(**values))
    except (ValueError, TypeError, OSError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
