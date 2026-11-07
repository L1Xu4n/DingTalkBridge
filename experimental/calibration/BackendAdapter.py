"""Reviewed live-channel adapter. No offline grant or automatic confirmation exists.

In UI-managed mode the adapter waits before importing the backend, allowing the
UI to assign its kill-on-close job before any backend helper can be created.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


def emit(value: dict) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def absolute_file(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError("absolute_existing_file_required")
    return path.resolve()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_binding(binding_path: Path, config_path: Path) -> dict:
    if binding_path.stat().st_size > 65536:
        raise ValueError("binding_too_large")
    binding = json.loads(binding_path.read_text(encoding="utf-8-sig"))
    if binding.get("schema") != "dingtalkbridge.roi-once-backend-binding.v1":
        raise ValueError("unsupported_backend_binding")
    expected = (
        (Path(__file__).resolve(), "adapter_script", "adapter_sha256"),
        (Path(sys.executable).resolve(), "python_exe", "python_sha256"),
        (config_path, "config_path", "config_sha256"),
    )
    for actual, path_key, hash_key in expected:
        recorded = absolute_file(binding[path_key])
        if os.path.normcase(str(actual)) != os.path.normcase(str(recorded)):
            raise ValueError("backend_path_mismatch")
        if sha256(actual) != binding[hash_key]:
            raise ValueError("backend_hash_mismatch")
    return binding


def safe_summary(summary: object) -> dict:
    """Only status/counters cross back into the UI; no message or image payloads."""
    if not isinstance(summary, dict):
        return {"status": "failed", "reason": "invalid_backend_summary"}
    status = summary.get("status")
    if status not in {"completed", "rejected", "cancelled", "failed"}:
        return {"status": "failed", "reason": "invalid_backend_status"}
    result: dict = {"status": status}
    for key in ("reason", "capture_status", "ocr_status"):
        value = summary.get(key)
        if isinstance(value, str) and value and len(value) <= 120:
            if all(char in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in value):
                result[key] = value
    for key in ("ingested_screen_contexts", "screen_contexts_added", "captured_text_items"):
        value = summary.get(key)
        if type(value) is int and 0 <= value <= 100000:
            result[key] = value
    if summary.get("verified_message_count") == 0 and type(summary.get("verified_message_count")) is int:
        result["verified_message_count"] = 0
    for key in ("attempt_consumed", "pixels_read", "ocr_started"):
        if type(summary.get(key)) is bool:
            result[key] = summary[key]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="DingTalkBridge local one-attempt UI adapter")
    parser.add_argument("mode", choices=("readiness", "run-once"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--ui-binding", required=True)
    parser.add_argument("--ui-managed", action="store_true")
    parser.add_argument("--click-event-handle", type=int)
    parser.add_argument("--native-event-handle", type=int)
    parser.add_argument("--revocation-event-handle", type=int)
    args = parser.parse_args()
    if args.ui_managed:
        preface = sys.stdin.buffer.readline(4097)
        if len(preface) > 4096 or not preface.endswith(b"\n"):
            raise ValueError("ui_job_preface_missing")
        if json.loads(preface) != {"type": "ui_job_ready"}:
            raise ValueError("ui_job_preface_invalid")
    elif args.mode == "run-once":
        raise ValueError("live_ui_job_required")
    # No bridge/backend import or helper creation is allowed before the preface.
    config_path = absolute_file(args.config)
    profile_path = absolute_file(args.profile)
    binding_path = absolute_file(args.ui_binding)
    validate_binding(binding_path, config_path)
    project = Path(__file__).resolve().parents[2]
    source = project / "src"
    if not source.is_dir():
        raise ValueError("local_backend_source_missing")
    sys.path.insert(0, str(source))
    from dingtalkbridge.config import load_config
    from dingtalkbridge import ocr

    config = load_config(config_path)
    if args.mode == "readiness":
        result = ocr.wgc_once_readiness(config, profile_path, backend_binding_path=binding_path)
        emit(result)
        return 0
    if any(value is None or value <= 0 for value in (args.click_event_handle, args.native_event_handle, args.revocation_event_handle)):
        raise ValueError("live_event_handles_required")
    from dingtalkbridge.store import Store

    # Store does not receive text until the backend completes all live-channel gates.
    with Store(config.database_path, config) as store:
        summary = ocr.run_user_confirmed_once(config, store, {
            "profile_path": str(profile_path),
            "backend_binding_path": str(binding_path),
            "click_event_handle": args.click_event_handle,
            "native_event_handle": args.native_event_handle,
            "revocation_event_handle": args.revocation_event_handle,
        })
    emit({"type": "result", "summary": safe_summary(summary)})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # No raw exceptions, paths containing user data, text, or images are returned.
        emit({"type": "result", "summary": {"status": "failed", "reason": "adapter_validation_or_backend_failure"}})
        raise SystemExit(3)
