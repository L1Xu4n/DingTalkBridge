"""Private bounded supervisor for the separately built native Controller.

This adapter never prints Controller output. A verified on-disk build manifest
is an accidental-build-mixing guard, not publisher authentication or proof that
the target callback ABI is correct. Native DTO records remain verification-
pending. Importing this module does not launch or attach to any process.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

from .config import Config
from .models import UTC, format_timestamp
from .native_receive import (
    MAX_FRAME_BYTES, REAL_SOURCE, SCHEMA, SyntheticEvent, consume_events,
)
from .store import Store


SYNTHETIC_ENVELOPE = "dingtalkbridge.synthetic-native-envelope.v1"
BUILD_SCHEMA = "dingtalkbridge.native-bootstrap-build.v1"
STATUS_TYPE = "bridge_native_status"
STARTUP_TIMEOUT_SECONDS = 30.0
HEARTBEAT_TIMEOUT_SECONDS = 15.0
STOP_TIMEOUT_SECONDS = 5.0
QUEUE_CAPACITY = 16
MAX_COUNTER = 2**63 - 1
IS_WINDOWS = os.name == "nt"
_RECEIVE_COUNTERS = (
    "received", "inserted", "duplicates_or_filtered", "policy_filtered", "expired",
    "invalid", "destination_rejected", "storage_errors", "stream_errors",
)
_CONTROLLER_COUNTERS = (
    "bootstrap_invocations", "callbacks_seen", "notifications_400", "messages_seen",
    "messages_enqueued", "messages_sent", "dropped", "decode_errors", "policy_rejections",
    "controller_lost", "pipe_failures", "identity_failures",
)


class CollectorError(ValueError):
    """Fixed, non-content diagnostic; never retains helper output or exceptions."""


def _fail(code: str) -> CollectorError:
    """Create a fixed diagnostic that cannot contain captured message text."""
    return CollectorError("native_controller:" + code)


def _option(args: Any, name: str, default: Any = None) -> Any:
    """Read an option from either CLI arguments or a test dictionary."""
    return args.get(name, default) if isinstance(args, Mapping) else getattr(args, name, default)


def _stamp() -> str:
    """Return the current UTC observation time."""
    return format_timestamp(datetime.now(UTC))


def default_controller_path(*, synthetic: bool = False) -> Path:
    """Resolve the matching production or synthetic Controller executable."""
    flavor = "synthetic" if synthetic else "production"
    executable = "BridgeControllerSynthetic.exe" if synthetic else "BridgeController.exe"
    return Path(__file__).resolve().parents[2] / "experimental" / "native-detour" / "bootstrap" / "bin" / "0.3.0" / flavor / executable


def _sha256(path: Path) -> str:
    """Hash a local artifact in bounded chunks before launching it."""
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys instead of silently replacing protocol fields."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("duplicate_json_fields")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    """Reject nonstandard JSON numbers such as NaN."""
    raise _fail("invalid_json")


def _json_object(raw: bytes, *, maximum: int = MAX_FRAME_BYTES) -> dict[str, Any]:
    """Decode one size-limited UTF-8 protocol object without exposing its contents."""
    if not isinstance(raw, bytes) or len(raw) > maximum:
        raise _fail("frame_limit")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except CollectorError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _fail("invalid_json") from None
    if not isinstance(value, dict):
        raise _fail("invalid_object")
    return value


def _validate_build(path: Path, *, synthetic: bool) -> None:
    """Check build flavor, target identity and executable/DLL hashes."""
    expected_exe = "BridgeControllerSynthetic.exe" if synthetic else "BridgeController.exe"
    expected_dll = "BridgeBootstrapSynthetic.dll" if synthetic else "BridgeBootstrap.dll"
    if path.name != expected_exe or not path.is_file():
        raise _fail("controller_unavailable")
    try:
        with (path.parent / "build.json").open("rb") as handle:
            manifest = _json_object(handle.read(65537), maximum=65536)
        if manifest.get("schema") != BUILD_SCHEMA or manifest.get("flavor") != ("synthetic" if synthetic else "production"):
            raise _fail("build_flavor_mismatch")
        if not synthetic:
            if manifest.get("synthetic_core") != "root":
                raise _fail("build_target_mismatch")
            _manifest_target(manifest)
        if synthetic:
            target = Path(str(manifest.get("target_path", "")))
            if target.name != "DingTalkBridgeNativeSyntheticHost.exe" or target.resolve().parent != path.parent.resolve():
                raise _fail("build_target_mismatch")
        for key, artifact in (("controller_sha256", path), ("dll_sha256", path.parent / expected_dll)):
            expected = manifest.get(key)
            if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None or _sha256(artifact) != expected:
                raise _fail("build_artifact_mismatch")
    except CollectorError:
        raise
    except Exception:
        raise _fail("build_unavailable") from None


def _manifest_target(manifest: dict[str, Any]) -> str:
    """Validate the locally compiled target path without requiring one installation drive."""
    target = manifest.get("target_path")
    if not isinstance(target, str) or not ntpath.isabs(target) or ntpath.basename(target).lower() != "dingtalk.exe":
        raise _fail("build_target_mismatch")
    return target


def configured_target_path() -> str | None:
    """Read the local build target for discovery; an unbuilt probe may match executable name only."""
    manifest_path = default_controller_path().parent / "build.json"
    if not manifest_path.exists():
        return None
    with manifest_path.open("rb") as handle:
        return _manifest_target(_json_object(handle.read(65537), maximum=65536))


def _validate_options(config: Config, store: Store, args: Any) -> tuple[Path, bool, str, int, int, float, str]:
    """Validate database, window, duration and session before touching the client."""
    if config.database_path.resolve() != store.path.resolve():
        raise _fail("database_mismatch")
    synthetic = _option(args, "synthetic", False)
    if not isinstance(synthetic, bool) or synthetic != (store.dataset_kind == "synthetic"):
        raise _fail("destination_mismatch")
    mode = _option(args, "mode", "count")
    if not isinstance(mode, str) or mode not in {"count", "capture"}:
        raise _fail("invalid_mode")
    pid, hwnd = _option(args, "pid"), _option(args, "hwnd")
    if isinstance(hwnd, str):
        try:
            hwnd = int(hwnd, 16 if hwnd.lower().startswith("0x") else 10)
        except ValueError:
            raise _fail("invalid_window") from None
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= 2**32 - 1:
        raise _fail("invalid_pid")
    if isinstance(hwnd, bool) or not isinstance(hwnd, int) or not 0 < hwnd <= 2**64 - 1:
        raise _fail("invalid_window")
    duration = _option(args, "duration", 0)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 <= duration <= 3600:
        raise _fail("invalid_duration")
    session = _option(args, "session")
    if session is None:
        session = "native-" + uuid4().hex
    if not isinstance(session, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,95}", session) is None:
        raise _fail("invalid_session")
    path = Path(_option(args, "controller") or default_controller_path(synthetic=synthetic)).expanduser().resolve()
    return path, synthetic, mode, pid, hwnd, float(duration), session


def _status_frame(frame: dict[str, Any], *, synthetic: bool) -> dict[str, Any]:
    """Accept only known bounded Controller status fields."""
    required = {"type", "event", "status", "error", "active", "worker_alive", "build_flavor"}
    allowed = required | set(_CONTROLLER_COUNTERS) | {"build_flavor"}
    if not required <= set(frame) or set(frame) - allowed or frame["type"] != STATUS_TYPE:
        raise _fail("invalid_status_fields")
    if not isinstance(frame["event"], str) or frame["event"] not in {"active", "heartbeat", "stopped"}:
        raise _fail("invalid_status_event")
    result = {"event": frame["event"]}
    for key in allowed - {"type", "event"}:
        if key not in frame:
            continue
        value = frame[key]
        maximum = 8 if key == "status" else 9 if key == "error" else 1 if key in {"active", "worker_alive"} else 2 if key == "build_flavor" else MAX_COUNTER
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise _fail("invalid_status_counter")
        result[key] = value
    if "build_flavor" in result and result["build_flavor"] != (2 if synthetic else 1):
        raise _fail("build_flavor_mismatch")
    return result


class _PrivateOutput:
    """Single bounded binary reader; neither raw frames nor errors are printed."""

    def __init__(self, stream: Any) -> None:
        """Prepare a bounded queue for private Controller stdout frames."""
        self.items: queue.Queue[tuple[str, bytes | None]] = queue.Queue(maxsize=QUEUE_CAPACITY)
        self.overflow = threading.Event()
        self.done = threading.Event()
        self.stop = threading.Event()
        self.stream = stream
        self.thread = threading.Thread(target=self._read, name="native-controller-output", daemon=True)

    def start(self) -> None:
        """Start the single background frame reader."""
        self.thread.start()

    def _put(self, kind: str, frame: bytes | None = None) -> bool:
        """Enqueue a frame or signal backpressure rather than using unbounded RAM."""
        if self.stop.is_set():
            return False
        try:
            self.items.put((kind, frame), timeout=0.1)
            return True
        except queue.Full:
            self.overflow.set()
            return False

    def _read(self) -> None:
        """Read complete bounded JSONL frames until shutdown or stream failure."""
        try:
            while not self.stop.is_set():
                line = self.stream.readline(MAX_FRAME_BYTES + 1)
                if not line:
                    return
                if not isinstance(line, bytes):
                    self._put("read_error")
                    return
                if len(line) > MAX_FRAME_BYTES:
                    self._put("oversized")
                    return
                if not line.endswith(b"\n"):
                    self._put("incomplete")
                    return
                if line.strip() and not self._put("frame", line):
                    return
        except Exception:
            self._put("read_error")
        finally:
            self.done.set()


def _request_stop(process: Any) -> bool:
    """Request native cooperative cleanup, then use bounded process fallbacks."""
    if process.poll() is not None:
        return True
    try:
        if process.stdin is not None:
            process.stdin.write(b"stop\n")
            process.stdin.flush()
            process.stdin.close()
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
        return True
    except Exception:
        if process.poll() is not None:
            return True
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass
        return False


def _write_status(config: Config, summary: dict[str, Any]) -> None:
    """Atomically replace the metadata-only heartbeat file."""
    path = config.data_dir / "collector-status.json"
    temporary = path.with_name(".native-status-" + uuid4().hex + ".json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(summary, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _consume_frame(config: Config, store: Store, frame: dict[str, Any], *, synthetic: bool, session: str) -> dict[str, int]:
    """Check session and real/synthetic provenance before writing a message."""
    if synthetic:
        if set(frame) != {"schema", "event"} or frame.get("schema") != SYNTHETIC_ENVELOPE or not isinstance(frame.get("event"), dict):
            raise _fail("synthetic_envelope_required")
        event = frame["event"]
        payload: Any = SyntheticEvent(MappingProxyType(event))
    else:
        if frame.get("schema") != SCHEMA:
            raise _fail("real_dto_required")
        event, payload = frame, frame
    if event.get("collector_session_id") != session:
        raise _fail("event_session_mismatch")
    return consume_events(config, store, [payload])


def _acquire_lock(config: Config, name: str = "native-collector.lock") -> Any:
    """Hold a collector or startup byte lock; lock files do not survive as live owners."""
    import msvcrt
    if name not in {"native-collector.lock", "native-autostart.lock"}:
        raise _fail("invalid_lock_name")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    handle = (config.data_dir / name).open("a+b")
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise _fail("collector_already_running") from None
    return handle


def request_stop(config: Config) -> dict[str, Any]:
    """Ask the current native collector to stop without terminating DingTalk."""
    config.data_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / "native-stop.request").touch()
    return {"stop_requested": True, "dingtalk_terminated": False}


def run_collect(config: Config, store: Store, args: Any = None) -> dict[str, Any]:
    """Run one caller-requested Controller; return metadata and counts only."""
    if not IS_WINDOWS:
        raise _fail("windows_required")
    path, synthetic, mode, pid, hwnd, duration, session = _validate_options(config, store, args)
    _validate_build(path, synthetic=synthetic)
    ownership = _acquire_lock(config)
    stop_path = config.data_dir / "native-stop.request"
    if not _option(args, "preserve_stop_request", False):
        stop_path.unlink(missing_ok=True)
    command = [str(path), "--pid", str(pid), "--hwnd", str(hwnd), "--duration", str(duration),
               "--session", session, "--mode", mode]
    if mode == "capture":
        command.append("--stream")
    summary: dict[str, Any] = {
        "status": "starting", "mode": "native_" + mode,
        "source": "synthetic" if synthetic else REAL_SOURCE,
        "collector_session_id": session, "controller_build_flavor": "synthetic" if synthetic else "production",
        "synthetic": synthetic, "structured_verification": "pending", "native_callback_verified": False,
        "account_identity": "unknown", "message_count": 0, "verified_message_count": 0,
        "native_counts": {key: 0 for key in _RECEIVE_COUNTERS}, "native_controller_counts": {},
        "dropped_stdout_frames": 0, "running": False, "controller_active_reported": False,
        "graceful_stop": True, "interrupted": False, "interval_seconds": 1,
        "heartbeat_at": _stamp(), "coverage": "native_event_dto_pending",
        "database_path_hash": hashlib.sha256(str(store.path.resolve()).casefold().encode("utf-8")).hexdigest(),
    }
    process = output = None
    active_since = last_heartbeat = None
    started = last_status_write = time.monotonic()
    stopped_reported = False
    try:
        startup = subprocess.STARTUPINFO() if IS_WINDOWS and hasattr(subprocess, "STARTUPINFO") else None
        if startup is not None:
            startup.lpDesktop = "winsta0\\default"
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   cwd=str(path.parent), text=False, bufsize=0,
                                   startupinfo=startup,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if process.stdout is None:
            raise _fail("stdout_unavailable")
        summary["running"] = True
        _write_status(config, summary)
        output = _PrivateOutput(process.stdout)
        output.start()
        while True:
            if stop_path.exists():
                summary["status"] = "stopped"
                break
            now = time.monotonic()
            if output.overflow.is_set():
                summary["dropped_stdout_frames"] = 1
                raise _fail("output_backpressure")
            if active_since is None and now - started >= STARTUP_TIMEOUT_SECONDS:
                raise _fail("startup_timeout")
            if last_heartbeat is not None and now - last_heartbeat >= HEARTBEAT_TIMEOUT_SECONDS:
                raise _fail("heartbeat_timeout")
            if active_since is not None and duration > 0 and now - active_since >= duration:
                summary["status"] = "completed"
                break
            try:
                kind, raw = output.items.get(timeout=0.1)
            except queue.Empty:
                if output.done.is_set():
                    break
                continue
            if kind != "frame" or raw is None:
                raise _fail("frame_limit" if kind == "oversized" else "invalid_frame")
            frame = _json_object(raw)
            if frame.get("type") == STATUS_TYPE:
                if stopped_reported:
                    raise _fail("invalid_status_order")
                status = _status_frame(frame, synthetic=synthetic)
                summary["native_controller_counts"] = {key: value for key, value in status.items() if key != "event"}
                if status["error"] != 0 or status["status"] in {6, 7, 8}:
                    raise _fail("controller_rejected")
                if status["event"] == "stopped":
                    if status["status"] != 5 or status["active"] != 0 or status["worker_alive"] != 0:
                        raise _fail("cleanup_not_confirmed")
                    stopped_reported = True
                    summary["controller_active_reported"] = False
                else:
                    if active_since is None and status["event"] != "active":
                        raise _fail("missing_active_event")
                    if status["status"] != 4 or status["active"] != 1:
                        raise _fail("invalid_active_status")
                    if active_since is None:
                        active_since = now
                    summary["controller_active_reported"] = True
                    summary["status"] = "available"
                    last_heartbeat = now
                summary["heartbeat_at"] = _stamp()
            else:
                if mode != "capture" or active_since is None or stopped_reported:
                    raise _fail("unexpected_data_frame")
                counts = _consume_frame(config, store, frame, synthetic=synthetic, session=session)
                for key, value in counts.items():
                    summary["native_counts"][key] = min(MAX_COUNTER, summary["native_counts"][key] + value)
                if any(counts[key] for key in ("invalid", "destination_rejected", "storage_errors", "stream_errors")):
                    raise _fail("dto_rejected")
            if now - last_status_write >= 1:
                _write_status(config, summary)
                last_status_write = now
        if active_since is None:
            raise _fail("controller_not_active")
        if summary["status"] not in {"completed", "stopped"}:
            if not stopped_reported:
                raise _fail("controller_output_closed")
            summary["status"] = "completed"
    except KeyboardInterrupt:
        summary.update(status="stopped", interrupted=True)
    except CollectorError as error:
        summary.update(status="failed", failure_reason=str(error).split(":", 1)[-1])
    except Exception:
        summary.update(status="failed", failure_reason="controller_operation_failed")
    finally:
        if process is not None:
            summary["graceful_stop"] = _request_stop(process)
            exit_code = process.poll()
            if isinstance(exit_code, int) and not isinstance(exit_code, bool) and -2**31 <= exit_code <= 2**32 - 1:
                summary["controller_exit_code"] = exit_code
                if exit_code != 0 and summary["status"] == "completed":
                    summary.update(status="failed", failure_reason="controller_exit_failed")
            if not summary["graceful_stop"] and summary["status"] == "completed":
                summary.update(status="failed", failure_reason="controller_stop_timeout")
        if output is not None:
            output.stop.set()
        if process is not None and process.stdout is not None:
            try:
                process.stdout.close()
            except Exception:
                pass
        summary["running"] = False
        summary["controller_active_reported"] = False
        summary["stopped_at"] = _stamp()
        summary["heartbeat_at"] = _stamp()
        summary["message_count"] = summary["native_counts"]["inserted"]
        try:
            _write_status(config, summary)
        except Exception:
            summary.update(status="failed", failure_reason="status_write_failed")
        ownership.close()
    return summary
