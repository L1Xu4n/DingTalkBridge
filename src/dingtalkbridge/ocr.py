"""Fail-closed, window-cropped local Windows OCR screen contexts.

Only Store.ingest receives recognized text. Status, exceptions and public return
values never contain raw OCR, UI labels, screenshots or arbitrary helper output.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import ntpath
import math
import secrets
import sys
import time
import os
from pathlib import Path
import queue
import re
import subprocess
import sysconfig
import threading
from typing import Any, Mapping
from uuid import uuid4

from .collector import DEFAULT_PROCESS_PATH, ensure_helper
from .models import parse_timestamp

SOURCE = "windows_ocr_screen"
COVERAGE = "cropped_visible_chat_component_only"
MAX_OUTPUT_BYTES = 512 * 1024
MAX_TEXT_BYTES = 128 * 1024


class OcrError(RuntimeError):
    """Controlled operational error; never embeds helper output or OCR text."""


# Match the existing collect CLI's controlled-error integration surface.
CollectorError = OcrError


def _option(args: Any, name: str, default: Any = None) -> Any:
    return args.get(name, default) if isinstance(args, Mapping) else getattr(args, name, default)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _integer(value: Any, *, handle: bool = False) -> int:
    if handle and isinstance(value, str) and len(value) <= 20:
        try:
            value = int(value, 16)  # Native geometry HWND strings are hexadecimal.
        except ValueError:
            raise OcrError("Invalid native window identity.") from None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 2**63 - 1:
        raise OcrError("Invalid native window identity.")
    return value


def _rect(value: Any) -> list[int]:
    if isinstance(value, Mapping):
        if all(key in value for key in ("left", "top", "right", "bottom")):
            value = [value[key] for key in ("left", "top", "right", "bottom")]
        elif all(key in value for key in ("x", "y", "width", "height")):
            if any(isinstance(value[key], bool) or not isinstance(value[key], int) for key in ("x", "y", "width", "height")):
                raise OcrError("Invalid physical-pixel rectangle.")
            value = [value["x"], value["y"], value["x"] + value["width"], value["y"] + value["height"]]
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(
        isinstance(item, bool) or not isinstance(item, int) or not -100000 <= item <= 100000 for item in value
    ):
        raise OcrError("Invalid physical-pixel rectangle.")
    result = list(value)
    if not 0 < result[2] - result[0] <= 16000 or not 0 < result[3] - result[1] <= 16000:
        raise OcrError("Invalid physical-pixel rectangle.")
    return result


def _contains(outer: list[int], inner: list[int]) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _overlap(a: list[int], b: list[int]) -> bool:
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def _bounded_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise OcrError("A geometry proof string is invalid.")
    return value


def _small_int(value: Any, maximum: int, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise OcrError("A geometry proof integer is invalid.")
    return value


def _proof_bool(value: Any) -> bool:
    if type(value) is not bool:
        raise OcrError("A geometry proof flag is invalid.")
    return value


def _canonical_owner(owner: Mapping[str, Any]) -> dict[str, Any]:
    keys = {"provider", "decision", "native_owner_hwnd", "owner_pid", "owner_class", "renderer",
            "source_scan", "native_source_descendant", "same_session", "verified_process_path", "owner_visible"}
    if not isinstance(owner, Mapping) or set(owner) != keys:
        raise OcrError("The native owner proof fields are incomplete.")
    if owner["provider"] != "native_owner" or owner["decision"] != "source_owner_verified":
        raise OcrError("The source native owner is unverified.")
    if any(owner[flag] is not True for flag in ("source_scan", "native_source_descendant", "same_session", "owner_visible")):
        raise OcrError("The source native owner relation is unverified.")
    cls = owner["owner_class"]
    renderer = _proof_bool(owner["renderer"])
    if cls not in {"CefBrowserWindow", "Chrome_WidgetWin_1", "Intermediate D3D Window"} or (renderer and cls != "Intermediate D3D Window"):
        raise OcrError("The source renderer proof is not narrowly scoped.")
    if owner["verified_process_path"] != DEFAULT_PROCESS_PATH:
        raise OcrError("The source owner image is not verified.")
    return {**{key: owner[key] for key in keys if key not in {"native_owner_hwnd", "owner_pid"}},
            "native_owner_hwnd": _integer(owner["native_owner_hwnd"], handle=True),
            "owner_pid": _small_int(owner["owner_pid"], 2**32-1, 1)}


def _proof_rect(value: Any, allow_empty: bool) -> list[int]:
    if not allow_empty:
        return _rect(value)
    if not isinstance(value, Mapping) or set(value) != {"x", "y", "width", "height"}:
        raise OcrError("An input proof rectangle is invalid.")
    if any(type(value[k]) is not int for k in value):
        raise OcrError("An input proof rectangle is invalid.")
    result = [value["x"], value["y"], value["x"]+value["width"], value["y"]+value["height"]]
    if any(not -100000 <= item <= 100000 for item in result) or not 0 <= value["width"] <= 16000 or not 0 <= value["height"] <= 16000:
        raise OcrError("An input proof rectangle is invalid.")
    return result


def _canonical_input(item: Mapping[str, Any], source_owners: Mapping[int, Mapping[str, Any]], main_pid: int) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise OcrError("The input proof is invalid.")
    provider = item.get("provider")
    if provider == "native_owner":
        return _canonical_owner(item)
    if provider == "uia":
        keys = {"provider", "reason", "decision", "control_type", "class_name", "framework_id", "depth",
                "rect", "is_offscreen", "source_scan", "native_owner_hwnd", "owner_pid", "owner_evidence"}
        if set(item) != keys:
            raise OcrError("The UIA input proof fields are incomplete.")
        reason, decision = item["reason"], item["decision"]
        if reason not in {"password", "edit_control", "input_marker", "writable_value_pattern", "unknown_value_pattern", "document_control"} or decision not in {"mask", "outside_source_tree"}:
            raise OcrError("The input decision is invalid.")
        source_scan = _proof_bool(item["source_scan"])
        owner = _integer(item["native_owner_hwnd"], handle=True)
        if source_scan and (decision != "mask" or owner not in source_owners):
            raise OcrError("A source input lacks an allowed source owner or mask.")
        if decision == "outside_source_tree" and (source_scan or owner in source_owners):
            raise OcrError("An editable source node cannot be ignored.")
        owner_pid = _small_int(item["owner_pid"], 2**32-1, 1)
        expected_owner_pid = source_owners[owner]["owner_pid"] if owner in source_owners else main_pid
        if owner_pid != expected_owner_pid:
            raise OcrError("The input HWND and owner PID contradict each other.")
        if item["owner_evidence"] not in {"explicit_native_handle", "verified_raw_ancestor"}:
            raise OcrError("The UIA native owner evidence is invalid.")
        control = _bounded_text(item["control_type"], 64)
        if not re.fullmatch(r"ControlType\.[A-Za-z]{1,40}", control):
            raise OcrError("The input control type is invalid.")
        if (reason in {"password", "edit_control", "input_marker"} or control in {"ControlType.Edit", "ControlType.ComboBox"}) and decision != "mask":
            raise OcrError("A strong input control must always be masked.")
        return {"provider": "uia", "reason": reason, "decision": decision, "control_type": control,
                "class_name": _bounded_text(item["class_name"], 256), "framework_id": _bounded_text(item["framework_id"], 32),
                "depth": _small_int(item["depth"], 60), "rect": _proof_rect(item["rect"], decision == "outside_source_tree"),
                "is_offscreen": _proof_bool(item["is_offscreen"]), "source_scan": source_scan,
                "native_owner_hwnd": owner, "owner_pid": _small_int(item["owner_pid"], 2**32-1, 1),
                "owner_evidence": item["owner_evidence"]}
    if provider == "msaa":
        keys = {"provider", "role", "protected", "depth", "rect", "native_owner_hwnd", "owner_pid",
                "owner_class", "owner_evidence", "owner_visible", "source_scan"}
        if set(item) != keys:
            raise OcrError("The MSAA input owner proof fields are incomplete.")
        protected = _proof_bool(item["protected"])
        role = _small_int(item["role"], 128, 1)
        if role not in {42, 46} and not protected:
            raise OcrError("The MSAA input role is invalid.")
        msaa_owner_pid = _small_int(item["owner_pid"], 2**32-1, 1)
        msaa_owner_hwnd = _integer(item["native_owner_hwnd"], handle=True)
        if msaa_owner_hwnd in source_owners and source_owners[msaa_owner_hwnd]["owner_pid"] != msaa_owner_pid:
            raise OcrError("The MSAA input HWND and owner PID contradict each other.")
        if item["owner_evidence"] != "msaa_window_from_accessible_object" or item["owner_visible"] is not True or item["source_scan"] is not False or msaa_owner_pid != main_pid:
            raise OcrError("The MSAA input owner relation is invalid.")
        return {"provider": "msaa", "role": role, "protected": protected, "decision": "mask",
                "depth": _small_int(item["depth"], 50), "rect": _rect(item["rect"]),
                "native_owner_hwnd": msaa_owner_hwnd,
                "owner_pid": msaa_owner_pid, "owner_class": _bounded_text(item["owner_class"], 128),
                "owner_evidence": item["owner_evidence"], "owner_visible": True, "source_scan": False}
    raise OcrError("The input proof provider is unknown.")


def validate_geometry(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize only the explicit structure-only native geometry contract."""
    if not isinstance(value, Mapping) or value.get("geometry_safe") is not True:
        raise OcrError("A safe message-display geometry proof is required.")
    if value.get("coordinate_space") != "physical_screen_pixels" or value.get("dpi_awareness") != "per_monitor_v2":
        raise OcrError("Physical-pixel DPI geometry is required.")
    if value.get("host_class") != "im_chat::DTIMChatComponent":
        raise OcrError("The semantic chat host is not verified.")
    path = value.get("verified_process_path")
    if not isinstance(path, str) or ntpath.normcase(ntpath.normpath(path)) != ntpath.normcase(DEFAULT_PROCESS_PATH):
        raise OcrError("The complete DingTalk image path is not verified.")
    if value.get("message_class") not in {"CefBrowserWindow", "Chrome_WidgetWin_1"}:
        raise OcrError("The message display native class is not verified.")
    result = {
        "pid": _integer(value.get("pid")), "hwnd": _integer(value.get("hwnd"), handle=True),
        "message_hwnd": _integer(value.get("message_hwnd"), handle=True),
        "host_class": value["host_class"], "message_class": value["message_class"],
        "verified_process_path": DEFAULT_PROCESS_PATH,
        "coordinate_space": "physical_screen_pixels", "dpi_awareness": "per_monitor_v2",
        "geometry_safe": True,
        "window_rect_screen": _rect(value.get("window_rect_screen")),
        "message_window_rect_screen": _rect(value.get("message_window_rect_screen")),
        "message_rect_screen": _rect(value.get("message_rect_screen")),
    }
    crop = result["message_rect_screen"]
    if not _contains(result["window_rect_screen"], crop) or not _contains(result["message_window_rect_screen"], crop):
        raise OcrError("The message crop exceeds its verified native display.")
    width, height = crop[2] - crop[0], crop[3] - crop[1]
    if width < 100 or height < 40 or width > 4096 or height > 4096 or width * height > 12000000:
        raise OcrError("The message crop dimensions are unsafe.")
    for key in ("composer_exclusions_screen", "editable_exclusions_screen"):
        values = value.get(key, [])
        if not isinstance(values, list) or len(values) > 64 or (key == "composer_exclusions_screen" and not values):
            raise OcrError("Verified composer exclusions are required.")
        result[key] = [_rect(item) for item in values]
        if any(_overlap(crop, item) for item in result[key]):
            raise OcrError("The message crop intersects an editable exclusion.")
    common = value.get("common_ancestor_hwnd")
    if common is not None:
        result["common_ancestor_hwnd"] = _integer(common, handle=True)
        if value.get("common_ancestor_class") not in {"DingChatWnd"}:
            raise OcrError("The common native ancestor class is not verified.")
        result["common_ancestor_class"] = value["common_ancestor_class"]
    if value.get("capture_source_policy") != "cef_message_hwnd_printwindow_only" or value.get("source_identity_scope") != "selected_native_source_subtree":
        raise OcrError("The capture source is not narrowly isolated.")
    if value.get("source_tree_scan_complete") is not True or type(value.get("source_tree_provider_errors")) is not int or value["source_tree_provider_errors"] != 0:
        raise OcrError("The complete source-tree input scan is not verified.")
    if type(value.get("source_tree_scan_nodes")) is not int or not 1 <= value["source_tree_scan_nodes"] <= 1500:
        raise OcrError("The source-tree scan is invalid.")
    if value.get("native_owner_proof") != "validated_hwnd_or_verified_raw_ancestor_in_selected_source_subtree":
        raise OcrError("The native source-owner proof is missing.")
    owners = value.get("source_native_owner_proof")
    source_pids = value.get("capture_source_process_ids")
    if not isinstance(owners, list) or not 1 <= len(owners) <= 128 or not isinstance(source_pids, list) or not 1 <= len(source_pids) <= 16:
        raise OcrError("The native source-owner set is invalid.")
    source_pids = sorted({_integer(item) for item in source_pids})
    if result["pid"] not in source_pids:
        raise OcrError("The selected source PID is missing.")
    root_owner_found = False
    for owner in owners:
        if not isinstance(owner, Mapping) or owner.get("provider") != "native_owner" or owner.get("decision") != "source_owner_verified":
            raise OcrError("A source native owner is unverified.")
        if any(owner.get(flag) is not True for flag in ("source_scan", "native_source_descendant", "same_session", "owner_visible")):
            raise OcrError("A source native owner is outside the verified subtree.")
        owner_pid = _integer(owner.get("owner_pid"))
        owner_hwnd = _integer(owner.get("native_owner_hwnd"), handle=True)
        owner_class = owner.get("owner_class")
        if owner_pid not in source_pids or owner.get("verified_process_path") != DEFAULT_PROCESS_PATH or owner_class not in {"CefBrowserWindow", "Chrome_WidgetWin_1", "Intermediate D3D Window"}:
            raise OcrError("A source native owner identity is not verified.")
        if owner_pid != result["pid"] and (owner.get("renderer") is not True or owner_class != "Intermediate D3D Window"):
            raise OcrError("A different PID requires the exact verified renderer allowance.")
        if owner.get("renderer") is True and owner_class != "Intermediate D3D Window":
            raise OcrError("The renderer allowance is not narrowly scoped.")
        if owner_hwnd == result["message_hwnd"] and owner_pid == result["pid"] and owner_class == result["message_class"]:
            root_owner_found = True
    if not root_owner_found:
        raise OcrError("The selected message HWND lacks an owner proof.")
    input_proof = value.get("input_geometry_evidence")
    if not isinstance(input_proof, list) or not 1 <= len(input_proof) <= 1500:
        raise OcrError("The input-source exclusion proof is missing.")
    result["capture_source_policy"] = "cef_message_hwnd_printwindow_only"
    result["source_identity_scope"] = "selected_native_source_subtree"
    result["source_tree_scan_complete"] = True
    result["source_tree_provider_errors"] = 0
    result["capture_source_process_ids"] = source_pids
    result["source_native_owner_count"] = len(owners)
    canonical_owners = [_canonical_owner(owner) for owner in owners]
    canonical_owners.sort(key=lambda item: item["native_owner_hwnd"])
    owner_map = {item["native_owner_hwnd"]: item for item in canonical_owners}
    if len(owner_map) != len(canonical_owners):
        raise OcrError("A native HWND has duplicate or contradictory owner proofs.")
    canonical_inputs = [_canonical_input(item, owner_map, result["pid"]) for item in input_proof]
    input_owners = sorted((item for item in canonical_inputs if item["provider"] == "native_owner"), key=lambda item: item["native_owner_hwnd"])
    if input_owners != canonical_owners or set(source_pids) != {item["owner_pid"] for item in canonical_owners}:
        raise OcrError("The source owner proof sets disagree.")
    input_masks = [item["rect"] for item in canonical_inputs if item.get("decision") == "mask"]
    if sorted(input_masks) != sorted(result["composer_exclusions_screen"] + result["editable_exclusions_screen"]):
        raise OcrError("The input masks and physical exclusions disagree.")
    ignored = sum(item.get("decision") == "outside_source_tree" for item in canonical_inputs)
    if _small_int(value.get("outside_source_unknown_ignored_count"), 1500) != ignored:
        raise OcrError("The outside-source proof is incomplete.")
    if _small_int(value.get("input_exclusion_count"), 1500, 1) != len(input_masks):
        raise OcrError("The input exclusion count is inconsistent.")
    composer_count = _small_int(value.get("composer_evidence_count"), len(input_masks), 1)
    if _small_int(value.get("geometry_provider_errors"), 0) != 0:
        raise OcrError("The geometry provider reported an error.")
    proof = value.get("geometry_evidence")
    if proof != "verified_qt_chat_host+cef_common_native_ancestor+confirmed_editor_boundary+no_edit_overlap":
        raise OcrError("The region lacks structural geometry evidence.")
    try:
        canonical_inputs.sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=True))
        proof_bundle = {"geometry_label": proof, "owners": canonical_owners, "inputs": canonical_inputs,
                        "source_pids": source_pids, "source_nodes": value["source_tree_scan_nodes"],
                        "source_complete": True, "source_errors": 0, "geometry_errors": 0,
                        "source_policy": value["capture_source_policy"], "identity_scope": value["source_identity_scope"],
                        "outside_source_ignored": ignored, "composer_count": composer_count}
        proof_bytes = json.dumps(proof_bundle, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("ascii")
        if len(proof_bytes) > 32768:
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        raise OcrError("The geometry proof is invalid.") from None
    result["geometry_evidence"] = "verified_native_message_display_and_editable_exclusions"
    result["geometry_evidence_sha256"] = hashlib.sha256(proof_bytes).hexdigest()
    return result


def _stop(process: subprocess.Popen[str], stop_file: Path) -> bool:
    if process.poll() is not None:
        return True
    stop_file.touch(exist_ok=True)
    try:
        process.wait(timeout=2)
        return True
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        return False


def _run_private(command: list[str], stop_file: Path, request: Mapping[str, Any] | None = None,
                 timeout: float = 30) -> str:
    """Bound CreateProcess and operation separately; keep all output pipe-only."""
    arrival: queue.Queue[Any] = queue.Queue(maxsize=1)
    abandoned = threading.Event()
    lock = threading.Lock()

    def launch() -> None:
        try:
            child = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8-sig", errors="strict",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except OSError:
            arrival.put(None)
            return
        with lock:
            late = abandoned.is_set()
            if not late:
                arrival.put(child)
        if late:
            try:
                _stop(child, stop_file)
            finally:
                stop_file.unlink(missing_ok=True)

    threading.Thread(target=launch, name="ocr-helper-launch", daemon=True).start()
    child = None
    failure = None
    try:
        try:
            child = arrival.get(timeout=min(timeout, 15))
        except queue.Empty:
            with lock:
                abandoned.set()
                try:
                    child = arrival.get_nowait()
                except queue.Empty:
                    pass
            raise OcrError("The OCR helper startup timed out.") from None
        if child is None:
            raise OcrError("Windows could not start the OCR helper.")
        encoded = json.dumps(request, ensure_ascii=False) if request is not None else ""
        try:
            output, _ = child.communicate(encoded, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise OcrError("The OCR helper operation timed out.") from None
        except (UnicodeError, OSError):
            raise OcrError("The OCR helper pipe could not be read.") from None
        if child.returncode != 0:
            raise OcrError("The OCR helper exited unsuccessfully.")
        if len(output.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise OcrError("The OCR helper output exceeded its bound.")
        return output
    except BaseException as exc:
        failure = exc
        raise
    finally:
        with lock:
            abandoned.set()
            if child is None:
                try:
                    child = arrival.get_nowait()
                except queue.Empty:
                    pass
        if child is not None:
            graceful = _stop(child, stop_file)
            if failure is not None:
                setattr(failure, "graceful_stop", graceful)
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe is not None:
                    pipe.close()


def _event(output: str, kind: str | None = None) -> Mapping[str, Any]:
    try:
        rows = [json.loads(line) for line in output.splitlines() if line.strip()]
        matches = [row for row in rows if isinstance(row, Mapping) and (kind is None or row.get("type") == kind)]
        if len(matches) != 1:
            raise ValueError
        return matches[0]
    except (ValueError, TypeError, RecursionError):
        raise OcrError("The OCR helper returned an invalid structured result.") from None


def _ocr_script() -> Path:
    names = ("Capture-Ocr.ps1", "SafeWindowCrop.cs", "LocalOcr.ps1")
    root = Path(__file__).resolve().parents[2] / "experimental" / "ocr"
    if not all((root / name).is_file() for name in names):
        root = Path(sysconfig.get_path("data")) / "share" / "dingtalkbridge" / "ocr"
    if not all((root / name).is_file() for name in names):
        raise OcrError("The local OCR helper sources are missing.")
    return root / "Capture-Ocr.ps1"


def screen_context_record(event: Mapping[str, Any], geometry: Mapping[str, Any], session: str) -> dict[str, Any] | None:
    """Preserve OCR as a partial observation, never as a verified chat message."""
    if event.get("status") != "available" or event.get("source") != SOURCE or event.get("kind") != "screen_context":
        return None
    language = event.get("language")
    if not isinstance(language, str) or not re.fullmatch(r"zh(?:-[A-Za-z0-9]{2,8}){1,3}", language, re.IGNORECASE):
        return None
    if event.get("counts_method") != "recognized_text_lines_and_whitespace_tokens":
        return None
    text = event.get("text")
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        return None
    try:
        observed = parse_timestamp(event.get("observed_at"))
    except ValueError:
        return None
    if abs((datetime.now(timezone.utc) - observed).total_seconds()) > 120:
        return None
    for key in ("line_count", "word_count"):
        if isinstance(event.get(key), bool) or not isinstance(event.get(key), int) or not 0 < event[key] <= 100000:
            return None
    conversation = f"ocr:visible-window:{session}:{geometry['pid']}:{geometry['hwnd']}"
    record_id = "ocr-screen-" + hashlib.sha256((conversation + "\n" + text).encode("utf-8")).hexdigest()
    return {
        "id": record_id, "conversation_id": conversation,
        "conversation_title": "Visible DingTalk OCR context (conversation unknown)",
        "sender_id": "unknown", "sender_name": "unknown", "text": text,
        "timestamp": event["observed_at"], "source": SOURCE, "confidence": 0.0,
        "metadata": {
            "kind": "screen_context", "is_message": False, "timestamp_kind": "observation_time",
            "account_identity": "unknown", "sender_identity": "unknown", "conversation_identity": "unknown",
            "collector_session_id": session, "scope_generation": 0, "pid": geometry["pid"],
            "window_id": str(geometry["hwnd"]), "sourceConfidence": 0,
            "numeric_confidence_available": False, "confidence_kind": "not_provided_by_windows_ocr",
            "coverage": COVERAGE, "history_available": False, "partial": True,
            "cropped": True, "content_classification": "unverified_ocr_screen_context",
            "evidence_region_counts": {"chat_component": event["line_count"]},
            "ocr": {"engine": "Windows.Media.Ocr", "language": language, "line_count": event["line_count"],
                    "word_count": event["word_count"], "counts_method": "recognized_text_lines_and_whitespace_tokens", "numeric_confidence_available": False},
            "capture": {"method": "PrintWindow_clipped_dc", "coordinate_space": "physical_screen_pixels",
                        "dpi_awareness": "per_monitor_v2", "message_hwnd": geometry["message_hwnd"],
                        "host_class": geometry["host_class"], "message_class": geometry["message_class"],
                        "crop_rect_screen": geometry["message_rect_screen"],
                        "composer_exclusion_count": len(geometry["composer_exclusions_screen"]),
                        "editable_exclusion_count": len(geometry["editable_exclusions_screen"])},
        },
    }


def _write_status(data_dir: Path, summary: Mapping[str, Any]) -> None:
    destination = data_dir / "collector-status.json"
    temporary = data_dir / f".ocr-status-{uuid4().hex}.json"
    try:
        temporary.write_text(json.dumps(dict(summary), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def run_collect(config: Any, store: Any, args: Any = None) -> dict[str, Any]:
    """Snapshot safe message pixels, OCR locally, optionally ingest; return metadata."""
    mode = _option(args, "mode", "snapshot")
    if mode not in {"snapshot", "probe"}:
        raise OcrError("OCR currently supports one-shot snapshot or probe.")
    pid = _option(args, "pid")
    if pid is not None:
        pid = _integer(pid)
    expected = _option(args, "process_path", DEFAULT_PROCESS_PATH)
    if not isinstance(expected, str) or ntpath.normcase(ntpath.normpath(expected)) != ntpath.normcase(DEFAULT_PROCESS_PATH):
        raise OcrError("OCR is limited to the validated DingTalk installation.")
    data_dir = Path(config.data_dir).resolve()
    database = Path(store.path).resolve()
    if database != Path(config.database_path).resolve():
        raise OcrError("OCR and MCP must use the same configured database.")
    data_dir.mkdir(parents=True, exist_ok=True)
    session = "ocr-" + uuid4().hex
    stop_file = data_dir / f".ocr-stop-{session}"
    include = bool(_option(args, "include_screen_context", False) or _option(args, "store_screen_context", False))
    result: dict[str, Any] = {
        "mode": mode, "status": "starting", "source": SOURCE, "scope": "current_visible_windows",
        "coverage": COVERAGE, "history_available": False, "message_count": 0, "verified_message_count": 0,
        "account_identity": "unknown", "collector_session_id": session, "scope_generation": 0,
        "database_path_hash": hashlib.sha256(str(database).casefold().encode("utf-8")).hexdigest(),
        "running": False, "collector_running": False, "interrupted": False, "graceful_stop": True,
        "desktop_kind": "unknown", "heartbeat_at": _stamp(), "snapshots": 0,
        "screen_context_storage": include, "ingested_screen_contexts": 0, "skipped_screen_contexts": 0,
        "line_count": 0, "word_count": 0, "safe_region_count": 0,
        "captured_text_items": 0, "cropSafety": False, "ui_labels_only": "not_assessed",
        "evidence_region_counts": {}, "confidence": 0, "numeric_confidence_available": False,
    }
    try:
        if os.name != "nt":
            raise OcrError("Windows OCR requires Windows.")
        script = _ocr_script()
        native = ensure_helper(data_dir)
        command = [str(native), "--mode", "regions", "--process-path", DEFAULT_PROCESS_PATH, "--stop-file", str(stop_file)]
        if pid is not None:
            command += ["--pid", str(pid)]
        regions_event = _event(_run_private(command, stop_file, timeout=20), "regions")
        desktop = regions_event.get("desktop_kind")
        result["desktop_kind"] = desktop if desktop in {"interactive_default", "isolated_sandbox", "other"} else "unknown"
        if result["desktop_kind"] != "interactive_default":
            result["status"] = "isolated_desktop" if desktop == "isolated_sandbox" else "unsafe_geometry"
            return result
        regions = regions_event.get("regions")
        if not isinstance(regions, list) or len(regions) != 1:
            result["status"] = "unsafe_geometry"
            result["reason"] = "no_unique_verified_message_region"
            return result
        try:
            geometry = validate_geometry(regions[0])
        except OcrError:
            result["status"] = "unsafe_geometry"
            result["reason"] = "message_region_or_editable_exclusions_not_verified"
            return result
        result["safe_region_count"] = 1
        result["process_ids"] = [geometry["pid"]]
        if pid is not None and geometry["pid"] != pid:
            raise OcrError("The geometry PID does not match the selected process.")
        if mode == "probe":
            result["status"] = "available"
            return result
        powershell = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not powershell.is_file():
            raise OcrError("Windows PowerShell 5.1 is unavailable.")
        command = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)]
        event = _event(_run_private(command, stop_file, {"geometry": geometry, "stop_file": str(stop_file)}, timeout=30))
        allowed_status = {"available", "no_text", "blank_capture", "capture_unavailable", "ocr_unavailable",
                          "unsafe_geometry", "target_changed", "dpi_unavailable", "stopped"}
        result["status"] = event.get("status") if event.get("status") in allowed_status else "ocr_unavailable"
        result["snapshots"] = 1
        if event.get("error_stage") in {"request", "native_compile", "capture", "winrt_activation", "image_stream", "image_decode", "recognize"}:
            result["error_stage"] = event["error_stage"]
        if event.get("error_type") in {"RuntimeException", "MethodInvocationException", "Exception", "TypeInitializationException", "InvalidOperationException", "ArgumentException", "COMException"}:
            result["error_type"] = event["error_type"]
        record = screen_context_record(event, geometry, session)
        if record is not None:
            # A second structure-only native pass must confirm the same source,
            # pixels and editor masks after recognition, before any persistence.
            post_command = [str(native), "--mode", "regions", "--process-path", DEFAULT_PROCESS_PATH,
                            "--pid", str(geometry["pid"]), "--stop-file", str(stop_file)]
            post_event = _event(_run_private(post_command, stop_file, timeout=20), "regions")
            post_regions = post_event.get("regions")
            try:
                if post_event.get("desktop_kind") != "interactive_default" or not isinstance(post_regions, list) or len(post_regions) != 1:
                    raise OcrError("The post-capture geometry is unavailable.")
                post_geometry = validate_geometry(post_regions[0])
                if post_geometry != geometry:
                    raise OcrError("The capture geometry or exclusions changed.")
            except OcrError:
                result["status"] = "unsafe_geometry"
                result["reason"] = "geometry_or_mask_changed_during_capture"
                result["post_capture_geometry_verified"] = False
                return result
            result["post_capture_geometry_verified"] = True
            result["cropSafety"] = True
            result["captured_text_items"] = event["line_count"]
            result["captured_text_characters"] = len(record["text"])
            record["metadata"]["desktop_kind"] = "interactive_default"
            record["metadata"]["scope"] = "current_visible_windows"
            record["metadata"]["capture"]["post_capture_geometry_verified"] = True
            record["metadata"]["capture"]["source_tree_scan_complete"] = True
            record["metadata"]["capture"]["source_native_owner_count"] = geometry["source_native_owner_count"]
            record["metadata"]["capture"]["source_policy"] = geometry["capture_source_policy"]
            record["metadata"]["capture"]["geometry_evidence_sha256"] = geometry["geometry_evidence_sha256"]
            result["line_count"] = event["line_count"]
            result["word_count"] = event["word_count"]
            result["language"] = event["language"]
            result["counts_method"] = event["counts_method"]
            result["last_observed_at"] = record["timestamp"]
            result["evidence_region_counts"] = {"chat_component": event["line_count"]}
            if include:
                if store.ingest(record):
                    result["ingested_screen_contexts"] += 1
                else:
                    result["skipped_screen_contexts"] += 1
        elif result["status"] == "available":
            result["status"] = "ocr_unavailable"
            result["reason"] = "invalid_or_empty_ocr_observation"
    except KeyboardInterrupt as exc:
        result["interrupted"] = True
        result["status"] = "stopped"
        result["graceful_stop"] = getattr(exc, "graceful_stop", True)
    except OcrError as exc:
        result["status"] = "collector_error"
        result["reason"] = "bounded_ocr_operation_unavailable"
        result["graceful_stop"] = getattr(exc, "graceful_stop", True)
    except Exception:
        result["status"] = "collector_error"
        result["reason"] = "ocr_snapshot_failed"
    finally:
        stop_file.unlink(missing_ok=True)
        result["running"] = False
        result["collector_running"] = False
        result["heartbeat_at"] = _stamp()
        result["stopped_at"] = _stamp()
        _write_status(data_dir, result)
    return result


# Independent live-UI-only one-shot policy. Strict OCR above is unchanged.
ONCE_POLICY = "user_confirmed_roi_once"
ONCE_READINESS_SCHEMA = "dingtalkbridge.wgc-once-readiness.v1"
ONCE_GRANT_SCHEMA = "dingtalkbridge.roi-once-live-grant.v1"
ONCE_BINDING_SCHEMA = "dingtalkbridge.roi-once-backend-binding.v1"


def _once_local_path(value: str | Path) -> Path:
    raw=str(value)
    if os.name=="nt":
        if not re.match(r"^[A-Za-z]:[\\/]",raw) or ":" in raw[2:] or raw.startswith(("\\\\","//")):
            raise OcrError("once_local_file_required")
        import ctypes
        if ctypes.WinDLL("kernel32").GetDriveTypeW(str(raw[:3]))!=3:
            raise OcrError("once_local_fixed_drive_required")
    elif not Path(raw).is_absolute() or raw.startswith("//"):
        raise OcrError("once_local_file_required")
    return Path(raw)


def _once_read_json(path: Path, maximum: int = 1024 * 1024) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError
        result = json.loads(data.decode("utf-8-sig"))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise OcrError("once_metadata_unreadable") from None


def _once_sha(path: Path) -> str:
    if not path.is_file() or not 0 < path.stat().st_size <= 64 * 1024 * 1024:
        raise OcrError("once_asset_unavailable")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _once_assets() -> dict[str, Path]:
    root = Path(__file__).resolve().parents[2]
    wgc = root / "experimental" / "wgc"
    ui = root / "experimental" / "calibration"
    if not wgc.is_dir():
        shared = Path(sysconfig.get_path("data")) / "share" / "dingtalkbridge"
        wgc, ui = shared / "wgc", shared / "calibration"
    return {
        "producer_cpp": wgc / "WgcOnce.cpp", "guard_header": wgc / "OnceGuard.h",
        "gpu_header": wgc / "GpuRoiMask.h", "producer_exe": wgc / "WgcOnce.exe",
        "build_manifest": wgc / "WgcOnce.build.json", "ui_exe": ui / "bin/DingTalkRegionCalibration.exe",
        "ui_source": ui / "RegionCalibration.cs", "adapter": ui / "BackendAdapter.py",
        "python_glue": Path(__file__).resolve(), "native_source": (root / "native/DingTalkUia.cs") if (root / "native/DingTalkUia.cs").is_file() else Path(sysconfig.get_path("data")) / "share/dingtalkbridge/native/DingTalkUia.cs",
    }


def wgc_once_backend_info() -> dict[str, Any]:
    """Build identity only. No UI, grant, pixels, capture or Store access."""
    assets = _once_assets()
    hashes: dict[str, str] = {}
    missing: list[str] = []
    for name, path in assets.items():
        if name == "build_manifest":
            continue
        try:
            hashes[name] = _once_sha(path)
        except (OcrError, OSError):
            missing.append(name)
    digest = hashlib.sha256(json.dumps(
        {"policy": ONCE_POLICY, "protocol": 1, "files": hashes}, sort_keys=True, separators=(",", ":")
    ).encode("ascii")).hexdigest()
    built = False
    if not missing:
        try:
            manifest = _once_read_json(assets["build_manifest"], 64 * 1024)
            built = manifest.get("schema") == "dingtalkbridge.wgc-once-build.v1" and all(
                manifest.get(name) == hashes[name]
                for name in ("producer_cpp", "guard_header", "gpu_header", "producer_exe")
            )
        except OcrError:
            pass
    return {"schema": "dingtalkbridge.wgc-once-backend-info.v1", "backend_digest": digest,
            "compiled_ready": built, "missing_assets": missing, "file_hashes": hashes,
            "ui_exe": str(assets["ui_exe"]), "producer_exe": str(assets["producer_exe"]),
            "capture_started": False, "pixels_read": False, "authorization_granted": False}


def _once_windows_process(pid: int) -> dict[str, Any]:
    import ctypes
    from ctypes import wintypes as w
    if os.name != "nt" or type(pid) is not int or not 0 < pid <= 2**32 - 1:
        raise OcrError("once_process_identity_unavailable")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    kernel.ProcessIdToSessionId.argtypes = [w.DWORD, ctypes.POINTER(w.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise OcrError("once_process_identity_unavailable")
    try:
        count = w.DWORD(32768)
        name = ctypes.create_unicode_buffer(count.value)
        times = [w.FILETIME() for _ in range(4)]
        exit_code, session = w.DWORD(), w.DWORD()
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(count)) or not kernel.GetProcessTimes(
            handle, *(ctypes.byref(value) for value in times)
        ) or not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259 or not kernel.ProcessIdToSessionId(pid, ctypes.byref(session)):
            raise OcrError("once_process_identity_unavailable")
        start = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return {"pid": pid, "path": name.value, "start_filetime": str(start), "session_id": session.value,
                "file_sha256": _once_sha(Path(name.value))}
    finally:
        kernel.CloseHandle(handle)


def _once_parent_pid(pid: int) -> int:
    import ctypes
    from ctypes import wintypes as w
    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD),
                    ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                    ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
    kernel.Process32FirstW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise OcrError("once_parent_identity_unavailable")
    try:
        item = Entry()
        item.dwSize = ctypes.sizeof(item)
        more = kernel.Process32FirstW(handle, ctypes.byref(item))
        for _ in range(10000):
            if not more:
                break
            if item.th32ProcessID == pid:
                return int(item.th32ParentProcessID)
            more = kernel.Process32NextW(handle, ctypes.byref(item))
    finally:
        kernel.CloseHandle(handle)
    raise OcrError("once_parent_identity_unavailable")


def _once_filetime_now() -> int:
    import ctypes
    from ctypes import wintypes as w
    value = w.FILETIME()
    ctypes.WinDLL("kernel32").GetSystemTimeAsFileTime(ctypes.byref(value))
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def _once_decimal_filetime(value: Any) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or not 16 <= len(value) <= 19:
        raise OcrError("once_invalid_filetime")
    result = int(value)
    if not 0 < result < 2**63:
        raise OcrError("once_invalid_filetime")
    return result


def _once_window_snapshot(target: Mapping[str, Any], roi: list[int]) -> dict[str, Any]:
    import ctypes
    from ctypes import wintypes as w
    class Rect(ctypes.Structure):
        _fields_ = [("left", w.LONG), ("top", w.LONG), ("right", w.LONG), ("bottom", w.LONG)]
    class Input(ctypes.Structure):
        _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]
    user = ctypes.WinDLL("user32", use_last_error=True)
    dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
    user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    if not user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4)):
        raise OcrError("once_physical_coordinates_unavailable")
    user.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(Rect)]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.GetWindowDisplayAffinity.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user.GetAncestor.argtypes = [w.HWND, w.UINT]
    user.GetAncestor.restype = w.HWND
    user.GetForegroundWindow.restype = w.HWND
    user.IsChild.argtypes = [w.HWND, w.HWND]
    user.IsWindowVisible.argtypes = [w.HWND]
    user.IsIconic.argtypes = [w.HWND]
    dwm.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
    dwm.DwmGetWindowAttribute.restype = ctypes.c_long
    handles = {key: _integer(target[key]["hwnd"], handle=True) for key in ("toplevel", "semantic", "message")}
    top = handles["toplevel"]
    windows = {}
    for key, hwnd in handles.items():
        pid, rect = w.DWORD(), Rect()
        cls = ctypes.create_unicode_buffer(256)
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != target["pid"] or not user.IsWindowVisible(hwnd) or not user.GetWindowRect(hwnd, ctypes.byref(rect)) or not user.GetClassNameW(hwnd, cls, 256):
            raise OcrError("once_window_identity_changed")
        bounds = _rect([rect.left, rect.top, rect.right, rect.bottom])
        if bounds != _rect(target[key]["bounds"]) or cls.value != target[key]["class_name"]:
            raise OcrError("once_window_geometry_changed")
        windows[key] = {"hwnd": hwnd, "class_name": cls.value, "bounds": bounds}
    if user.GetAncestor(top, 2) != top or user.IsIconic(top) or not all(user.IsChild(top, handles[key]) for key in ("semantic", "message")):
        raise OcrError("once_window_ancestry_changed")
    if not _contains(windows["message"]["bounds"], roi) or not _contains(windows["semantic"]["bounds"], roi):
        raise OcrError("once_roi_outside_selected_message")
    affinity, cloaked, frame = w.DWORD(), w.DWORD(1), Rect()
    if not user.GetWindowDisplayAffinity(top, ctypes.byref(affinity)) or affinity.value != 0:
        raise OcrError("once_capture_protection_unknown_or_restricted")
    if dwm.DwmGetWindowAttribute(top, 14, ctypes.byref(cloaked), 4) != 0 or cloaked.value or dwm.DwmGetWindowAttribute(top, 9, ctypes.byref(frame), ctypes.sizeof(frame)) != 0:
        raise OcrError("once_dwm_unavailable")
    frame_bounds = _rect([frame.left, frame.top, frame.right, frame.bottom])
    inp = Input()
    inp.cbSize = ctypes.sizeof(inp)
    if not user.GetLastInputInfo(ctypes.byref(inp)):
        raise OcrError("once_input_epoch_unavailable")
    return {"windows": windows, "dwm_bounds": frame_bounds, "display_affinity": 0,
            "foreground_hwnd": int(user.GetForegroundWindow() or 0), "last_input_tick": int(inp.dwTime)}


def _once_native_rect(value: Any, *, allow_empty_native: bool = False) -> list[int]:
    if not allow_empty_native:
        return _rect(value)
    if not isinstance(value, Mapping) or any(type(value.get(k)) is not int for k in ("x", "y", "width", "height")):
        raise OcrError("once_native_empty_untyped")
    x,y,w,h = (value[k] for k in ("x", "y", "width", "height"))
    if not -100000 <= x <= 100000 or not -100000 <= y <= 100000 or not 0 <= w <= 16000 or not 0 <= h <= 16000:
        raise OcrError("once_native_bounds_invalid")
    return [x,y,x+w,y+h]


def _once_intersection(a: list[int], b: list[int]) -> list[int] | None:
    r = [max(a[0],b[0]),max(a[1],b[1]),min(a[2],b[2]),min(a[3],b[3])]
    return r if r[2]>r[0] and r[3]>r[1] else None


def _once_area(a: list[int] | None) -> int:
    return (a[2]-a[0])*(a[3]-a[1]) if a else 0


def _once_manual_technical(candidate: Mapping[str, Any]) -> list[list[int]]:
    """Independent manual policy: close every classified error by exact native evidence."""
    gate, alt, coverage = candidate.get("calibration_gate",{}), candidate.get("alternative_technical_evidence",{}), candidate.get("coverage",{})
    if gate.get("schema_version")!=1 or gate.get("unclassified_provider_error_count")!=0 or coverage.get("native_child_errors")!=0:
        raise OcrError("once_scene_unclassified_errors")
    errors = gate.get("technical_errors")
    if not isinstance(errors,list) or not 0 < len(errors) <= 32 or gate.get("classified_provider_error_count")!=len(errors) or coverage.get("provider_errors")!=len(errors):
        raise OcrError("once_alternative_error_count_invalid")
    if alt.get("metadata_only") is not True or alt.get("not_applied_to_strict_masks_or_gate") is not True or alt.get("fresh_binding_verified") is not True or alt.get("capture_authorized") is not False or alt.get("superset_padding_pixels")!=8:
        raise OcrError("once_alternative_gate_invalid")
    byref = {}
    for error in errors:
        if not isinstance(error,dict) or error.get("geometry_error") is not True or error.get("technical_error") is not True or error.get("strong_input") is True:
            raise OcrError("once_alternative_strong_or_unknown_error")
        ref = error.get("error_ref")
        if not isinstance(ref,str) or not re.fullmatch(r"geometry_error_[0-9]{1,4}",ref) or ref in byref:
            raise OcrError("once_alternative_error_ref_invalid")
        if (error.get("provider"),error.get("error_code"),error.get("error_category")) not in {
            ("uia","invalid_input_bbox","coordinates"),("msaa","accessible_owner_not_in_verified_visible_scene","owner_identity")}:
            raise OcrError("once_alternative_error_kind_invalid")
        if error.get("provider")=="uia" and (error.get("strong_input") is not False or error.get("input_reason") not in {"writable_value_pattern","unknown_value_pattern"}):
            raise OcrError("once_alternative_strong_or_unknown_error")
        byref[ref]=error
    top = candidate["toplevel"]; pid = _integer(top["pid"]); topid = _integer(top["hwnd"],handle=True)
    roi = _rect(candidate["message_roi_screen"]); owners = alt.get("owners")
    if not isinstance(owners,list) or len(owners)!=len(errors):
        raise OcrError("once_alternative_owner_count_invalid")
    seen=set(); masks=[]
    for owner in owners:
        if not isinstance(owner,dict): raise OcrError("once_alternative_owner_invalid")
        for key in ("selected_pid_matches","image_path_matches","same_session","is_window","is_top_or_child","gwr_call_succeeded","native_bounds_nonnegative","native_parent_client_clip_chain_complete","ancestor_chain_reached_selected_top"):
            if owner.get(key) is not True: raise OcrError("once_alternative_owner_identity_invalid")
        if owner.get("owner_pid")!=pid or owner.get("ws_child") is not True or owner.get("ws_ex_layered") is not False or owner.get("ws_ex_transparent") is not False or owner.get("rgn_used_to_shrink_mask") is not False or owner.get("readonly_semantics_inferred") is not False or owner.get("applied_as_override") is not False:
            raise OcrError("once_alternative_owner_flags_invalid")
        hwnd = _integer(owner["native_owner_hwnd"],handle=True)
        refs,bindings = owner.get("resolves_error_refs"),owner.get("source_error_bindings")
        if not isinstance(refs,list) or len(refs)!=1 or not isinstance(bindings,list) or len(bindings)!=1 or refs[0] not in byref or refs[0] in seen or bindings[0]!=byref[refs[0]]:
            raise OcrError("once_alternative_error_binding_invalid")
        error=byref[refs[0]]
        if _integer(error.get("native_owner_hwnd"),handle=True)!=hwnd or error.get("owner_pid")!=pid:
            raise OcrError("once_alternative_owner_error_mismatch")
        seen.add(refs[0])
        raw = _once_native_rect(owner["window_rect_screen"],allow_empty_native=True)
        empty = raw[2]==raw[0] or raw[3]==raw[1]
        if owner.get("proven_empty_native_bounds") is not empty or owner.get("gwr_valid") is not (not empty):
            raise OcrError("once_alternative_empty_proof_invalid")
        superset=list(raw)
        if not empty and owner.get("dwm_hresult")==0:
            d=_rect(owner["dwm_frame_rect_screen"]);superset=[min(raw[0],d[0]),min(raw[1],d[1]),max(raw[2],d[2]),max(raw[3],d[3])]
        if _once_native_rect(owner["candidate_superset_screen"],allow_empty_native=True)!=superset:
            raise OcrError("once_alternative_superset_invalid")
        padded=[superset[0]-8,superset[1]-8,superset[2]+8,superset[3]+8]
        if owner.get("superset_padding_pixels")!=8 or _rect(owner["padded_superset_screen"])!=padded or owner.get("padded_superset_roi_intersection_area")!=_once_area(_once_intersection(padded,roi)):
            raise OcrError("once_alternative_mask_shrunk")
        chain=owner.get("ancestor_chain")
        if not isinstance(chain,list) or not 2<=len(chain)<=32 or _integer(chain[0]["hwnd"],handle=True)!=hwnd or _integer(chain[-1]["hwnd"],handle=True)!=topid:
            raise OcrError("once_alternative_chain_invalid")
        for n,node in enumerate(chain):
            for key in ("is_window","image_path_matches","same_session","selected_top_relation","client_call_succeeded","client_bounds_nonnegative"):
                if node.get(key) is not True: raise OcrError("once_alternative_chain_identity_invalid")
            if node.get("pid")!=pid or node.get("ws_ex_layered") is not False or node.get("ws_ex_transparent") is not False:
                raise OcrError("once_alternative_chain_flags_invalid")
            wr=_once_native_rect(node["window_rect_screen"],allow_empty_native=True);cr=_once_native_rect(node["client_rect_screen"],allow_empty_native=True)
            if n==0 and (wr!=raw or node.get("native_class")!=owner.get("native_class")):
                raise OcrError("once_alternative_chain_owner_mismatch")
            if n<len(chain)-1 and (node.get("ws_child") is not True or node.get("parent_is_real_native_parent") is not True or _integer(node["parent_hwnd"],handle=True)!=_integer(chain[n+1]["hwnd"],handle=True)):
                raise OcrError("once_alternative_parent_invalid")
            if node.get("bounds_valid") is not (wr[2]>wr[0] and wr[3]>wr[1]) or node.get("client_bounds_valid") is not (cr[2]>cr[0] and cr[3]>cr[1]):
                raise OcrError("once_alternative_chain_bounds_invalid")
        if _once_native_rect(chain[-1]["window_rect_screen"],allow_empty_native=True)!=_rect(top["window_rect_screen"]):
            raise OcrError("once_alternative_top_bounds_invalid")
        masks.append(padded)
    if seen!=set(byref): raise OcrError("once_alternative_errors_unresolved")
    strong=alt.get("known_strong_input_proof"); expected=gate.get("known_strong_input_rects_screen")
    if alt.get("known_strong_input_geometry_complete") is not True or not isinstance(strong,list) or not isinstance(expected,list) or not 0<len(strong)<=512 or len(strong)!=len(expected):
        raise OcrError("once_known_input_geometry_invalid")
    for proof,rect in zip(strong,expected):
        r=_rect(proof["rect"])
        if proof.get("technical_bbox_owner_valid") is not True or proof.get("owner_pid")!=pid or r!=_rect(rect) or proof.get("roi_intersection_area")!=_once_area(_once_intersection(r,roi)):
            raise OcrError("once_known_input_geometry_invalid")
        _integer(proof.get("native_owner_hwnd"),handle=True)
        if proof.get("provider")=="uia" and proof.get("reason") not in {"password","edit_control","input_marker"}: raise OcrError("once_known_input_kind_invalid")
        if proof.get("provider")=="msaa" and proof.get("role") not in {42,46,"protected"}: raise OcrError("once_known_input_kind_invalid")
        masks.append([r[0]-2,r[1]-2,r[2]+2,r[3]+2])
    return masks


def _once_canonical_scene(scene: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Bind real identities/masks/classified errors, never observation timestamps."""
    if not isinstance(scene, Mapping) or scene.get("type") != "scene" or scene.get("metadata_only") is not True or scene.get("text_properties_read") is not False:
        raise OcrError("once_scene_metadata_invalid")
    if scene.get("coordinate_space") != "physical_screen_pixels" or scene.get("dpi_awareness") != "per_monitor_v2" or scene.get("desktop_kind") != "interactive_default":
        raise OcrError("once_scene_coordinates_invalid")
    candidate = scene.get("candidate_scene")
    if not isinstance(candidate, dict):
        raise OcrError("once_scene_missing")
    gate = candidate.get("calibration_gate")
    coverage = candidate.get("coverage")
    if not isinstance(gate, dict) or not isinstance(coverage, dict):
        raise OcrError("once_scene_classification_missing")
    if gate.get("technical_complete") is not True or gate.get("known_strong_input_geometry_complete") is not True:
        _once_manual_technical(candidate)
    else:
        if gate.get("schema_version") != 1 or gate.get("technical_errors") != [] or gate.get("unclassified_provider_error_count") != 0 or coverage.get("provider_errors") != 0 or coverage.get("native_child_errors") != 0 or coverage.get("complete") is not True:
            raise OcrError("once_scene_technical_errors")
    if candidate.get("layout_whitelist_exemptions") != 0 or candidate.get("outside_source_tree_ignored_count") != 0 or candidate.get("root_window_value_exception_applied") is not False or candidate.get("gpu_redaction_before_cpu_required") is not True:
        raise OcrError("once_scene_exemptions_forbidden")
    strong = gate.get("known_strong_input_rects_screen")
    if not isinstance(strong, list) or len(strong) > 512:
        raise OcrError("once_known_input_geometry_invalid")
    for item in strong:
        _rect(item)
    def clean(value: Any, depth: int = 0) -> Any:
        if depth > 20:
            raise OcrError("once_scene_too_deep")
        if isinstance(value, Mapping):
            if len(value) > 128:
                raise OcrError("once_scene_too_large")
            return {str(k): clean(v, depth + 1) for k, v in sorted(value.items())
                    if k not in {"observed_at", "heartbeat_at", "elapsed_ms", "scan_duration_ms"}}
        if isinstance(value, list):
            if len(value) > 1500:
                raise OcrError("once_scene_too_large")
            # Preserve geometry/ancestry order. Unknown lists are not sets.
            return [clean(item, depth + 1) for item in value]
        if isinstance(value, str):
            return _bounded_text(value, 2048)
        if value is None or type(value) in (int, bool):
            return value
        if type(value) is float and math.isfinite(value):
            return value
        raise OcrError("once_scene_value_invalid")
    canonical = clean({"candidate_scene": candidate, "coordinate_space": scene["coordinate_space"],
                       "dpi_awareness": scene["dpi_awareness"], "capture_backend_policy": scene.get("capture_backend_policy")})
    encoded = json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    if len(encoded) > 1024 * 1024:
        raise OcrError("once_scene_too_large")
    return hashlib.sha256(encoded).hexdigest(), candidate


def _once_prepare(config: Any, profile_path: str | Path, backend_binding_path: str | Path | None) -> tuple[dict[str, Any], dict[str, Any]]:
    info = wgc_once_backend_info()
    response: dict[str, Any] = {
        "schema": ONCE_READINESS_SCHEMA, "ready": False, "backend_digest": info["backend_digest"],
        "profile_sha256": "", "scene_binding_sha256": "", "capture_started": False, "pixels_read": False,
        "authorization_granted": False, "usable": False, "reasons": [],
        "technical_complete": False, "known_strong_input_geometry_complete": False,
        "current_scene_status": "unavailable",
    }
    context: dict[str, Any] = {"build": info}
    reasons = response["reasons"]
    if not info["compiled_ready"]:
        reasons.append("backend_not_compiled_or_stale")
    try:
        path = _once_local_path(profile_path).resolve(strict=True)
        profile = _once_read_json(path, 64 * 1024)
        response["profile_sha256"] = _once_sha(path)
        context.update(profile=profile, profile_path=path)
        if profile.get("schema") != "dingtalkbridge.region-calibration-descriptor.v1" or profile.get("intent") != "pending_semantic_calibration_only":
            raise OcrError("once_descriptor_schema_invalid")
        if profile.get("capture_permitted") is not False or profile.get("authorization_granted", False) is not False or profile.get("usable", False) is not False:
            raise OcrError("once_pending_descriptor_cannot_authorize")
        issued, expires = parse_timestamp(profile.get("issued_utc")), parse_timestamp(profile.get("expires_utc"))
        now = datetime.now(timezone.utc)
        if issued > now + __import__("datetime").timedelta(seconds=2) or expires <= now or not 0 < (expires-issued).total_seconds() <= 300:
            reasons.append("descriptor_expired")
        target = profile["target"]
        if not isinstance(target, dict) or ntpath.normcase(str(target.get("path", ""))) != ntpath.normcase(DEFAULT_PROCESS_PATH):
            raise OcrError("once_target_path_invalid")
        target = dict(target)
        target["pid"] = _integer(target["pid"])
        roi = _rect(profile["candidate_roi_screen"])
        process = _once_windows_process(target["pid"])
        if ntpath.normcase(process["path"]) != ntpath.normcase(DEFAULT_PROCESS_PATH) or process["file_sha256"] != target["file_sha256"] or process["session_id"] != target["session_id"]:
            raise OcrError("once_target_process_changed")
        if _once_decimal_filetime(target.get("process_start_filetime")) != int(process["start_filetime"]):
            raise OcrError("once_target_process_restarted")
        snapshot = _once_window_snapshot(target, roi)
        if snapshot["dwm_bounds"] != _rect(profile["dwm_frame_bounds_screen"]):
            raise OcrError("once_dwm_binding_changed")
        context.update(target=target, process=process, roi=roi, snapshot=snapshot)
        scene_source = profile["sources"]["scene"]
        scene_path = _once_local_path(scene_source["path"]).resolve(strict=True)
        if _once_sha(scene_path) != scene_source["sha256"]:
            raise OcrError("once_scene_source_changed")
        stored_scene = _once_read_json(scene_path, 8 * 1024 * 1024)
        status_value = stored_scene.get("status")
        response["current_scene_status"] = status_value if status_value in {"no_safe_unmasked_region", "requires_reviewed_gpu_backend", "unsafe_geometry", "incomplete_scene"} else "unknown"
        try:
            _, candidate = _once_canonical_scene(stored_scene)
        except OcrError as exc:
            reasons.append(str(exc))
            candidate = stored_scene.get("candidate_scene", {})
        gate = candidate.get("calibration_gate", {}) if isinstance(candidate, dict) else {}
        response["technical_complete"] = False
        response["known_strong_input_geometry_complete"] = False
        try:
            _once_canonical_scene(stored_scene)
            response["technical_complete"] = True
            response["known_strong_input_geometry_complete"] = True
        except OcrError:
            pass
        if response["technical_complete"] and response["known_strong_input_geometry_complete"]:
            native = ensure_helper(Path(config.data_dir))
            stop = Path(config.data_dir) / (".once-scene-" + uuid4().hex)
            live = _event(_run_private([str(native), "--mode", "scene", "--process-path", DEFAULT_PROCESS_PATH, "--pid", str(target["pid"])], stop, timeout=20), "scene")
            scene_hash, candidate = _once_canonical_scene(live)
            initial_hash, _ = _once_canonical_scene(stored_scene)
            if scene_hash != initial_hash:
                reasons.append("descriptor_scene_changed")
            top = candidate["toplevel"]
            if _integer(top["hwnd"], handle=True) != _integer(target["toplevel"]["hwnd"], handle=True) or top["pid"] != target["pid"] or top["session_id"] != target["session_id"]:
                raise OcrError("once_scene_target_changed")
            if top.get("display_affinity_read_ok") is not True or top.get("display_affinity") != 0 or top.get("visible") is not True or top.get("iconic") is not False or top.get("dwm_cloaked") != 0:
                raise OcrError("once_scene_protection_or_visibility_invalid")
            if _integer(candidate["selected_cef_hwnd"], handle=True) != _integer(target["message"]["hwnd"], handle=True) or _integer(candidate["selected_semantic_hwnd"], handle=True) != _integer(target["semantic"]["hwnd"], handle=True):
                raise OcrError("once_selected_ancestry_changed")
            if _rect(top["window_rect_screen"]) != snapshot["windows"]["toplevel"]["bounds"] or _rect(top["dwm_frame_rect_screen"]) != snapshot["dwm_bounds"]:
                raise OcrError("once_scene_coordinate_binding_changed")
            response["scene_binding_sha256"] = scene_hash
            context.update(scene=live, scene_candidate=candidate, scene_hash=scene_hash, native=native)
        else:
            reasons.append("technical_scene_gate_closed")
        wgc_source = profile["sources"]["wgc"]
        wgc_path = _once_local_path(wgc_source["path"]).resolve(strict=True)
        if _once_sha(wgc_path) != wgc_source["sha256"]:
            raise OcrError("once_wgc_metadata_changed")
        wgc = _once_read_json(wgc_path, 256 * 1024)
        if wgc.get("item_created") is not True or wgc.get("roi_mapping_valid") is not True or wgc.get("coordinate_mapping_ambiguous") is not False or wgc.get("display_affinity") != 0 or wgc.get("capture_started") is not False:
            raise OcrError("once_wgc_coordinate_proof_invalid")
        if wgc.get("toplevel_pid") != target["pid"] or wgc.get("toplevel_hwnd") != snapshot["windows"]["toplevel"]["hwnd"]:
            raise OcrError("once_wgc_target_changed")
        basis = _rect(wgc["dwm_frame_bounds_screen"] if wgc["coordinate_basis"] == "dwm_extended_frame_bounds" else wgc["window_rect_screen"])
        if basis != snapshot["dwm_bounds"] or not _contains(basis, roi):
            raise OcrError("once_wgc_origin_changed")
        context.update(wgc=wgc, capture_basis=basis)
    except (OcrError, KeyError, TypeError, ValueError, OSError) as exc:
        reasons.append(str(exc) if isinstance(exc, OcrError) else "descriptor_or_identity_invalid")
    try:
        if backend_binding_path is None:
            raise OcrError("backend_binding_required")
        binding_path = _once_local_path(backend_binding_path).resolve(strict=True)
        binding = _once_read_json(binding_path, 64 * 1024)
        if binding.get("schema") != ONCE_BINDING_SCHEMA:
            raise OcrError("backend_binding_schema_invalid")
        assets = _once_assets()
        checks = (("python_exe", "python_sha256", Path(sys.executable)),
                  ("adapter_script", "adapter_sha256", assets["adapter"]),
                  ("ui_exe", "ui_sha256", assets["ui_exe"]))
        for path_key, hash_key, expected in checks:
            actual = _once_local_path(binding[path_key]).resolve(strict=True)
            if ntpath.normcase(str(actual)) != ntpath.normcase(str(expected.resolve())) or _once_sha(actual) != binding[hash_key]:
                raise OcrError("backend_binary_binding_changed")
        from .config import load_config
        config_path = _once_local_path(binding["config_path"]).resolve(strict=True)
        if _once_sha(config_path) != binding["config_sha256"] or load_config(config_path) != config:
            raise OcrError("backend_config_binding_changed")
        if binding.get("expected_backend_digest") != info["backend_digest"]:
            raise OcrError("backend_digest_changed")
        context.update(binding=binding, binding_path=binding_path)
    except (OcrError, KeyError, TypeError, ValueError, OSError) as exc:
        reasons.append(str(exc) if isinstance(exc, OcrError) else "backend_binding_invalid")
    response["reasons"] = sorted(set(reasons))
    response["ready"] = not response["reasons"] and info["compiled_ready"]
    context["readiness"] = response
    return response, context


def wgc_once_readiness(config: Any, profile_path: str | Path, backend_binding_path: str | Path | None = None) -> dict[str, Any]:
    """Metadata only; pending descriptors and old authorization never grant access."""
    try:
        response, _ = _once_prepare(config, profile_path, backend_binding_path)
        return response
    except Exception:
        info = wgc_once_backend_info()
        return {"schema": ONCE_READINESS_SCHEMA, "ready": False, "backend_digest": info["backend_digest"],
                "profile_sha256": "", "scene_binding_sha256": "", "capture_started": False,
                "pixels_read": False, "authorization_granted": False, "usable": False,
                "technical_complete": False, "known_strong_input_geometry_complete": False,
                "current_scene_status": "unavailable", "reasons": ["readiness_unavailable"]}


class _OnceAttempt:
    """One in-memory attempt; no disk grant loading or retry operation exists."""
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.spent = False
        self.challenge = secrets.token_hex(32)

    def consume(self) -> None:
        with self.lock:
            if self.spent:
                raise OcrError("once_attempt_already_consumed")
            self.spent = True


def _once_live_parent(context: Mapping[str, Any]) -> dict[str, Any]:
    binding = context["binding"]
    parent = _once_windows_process(_once_parent_pid(os.getpid()))
    if ntpath.normcase(parent["path"]) != ntpath.normcase(str(Path(binding["ui_exe"]).resolve())) or parent["file_sha256"] != binding["ui_sha256"]:
        raise OcrError("live_calibration_ui_parent_required")
    me = _once_windows_process(os.getpid())
    if me["session_id"] != parent["session_id"] or me["session_id"] != context["process"]["session_id"]:
        raise OcrError("live_ui_session_mismatch")
    return {"ui": parent, "python": me}


def _once_wait_event(handle: Any, *, signaled: bool) -> None:
    import ctypes
    from ctypes import wintypes as w
    if type(handle) is not int or handle <= 0:
        raise OcrError("live_ui_kernel_event_required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    result = kernel.WaitForSingleObject(handle, 0)
    if result != (0 if signaled else 258):
        raise OcrError("live_ui_kernel_event_invalid_or_revoked")


def _once_read_line(stream: Any, timeout: float, maximum: int = 256 * 1024) -> str:
    arrival: queue.Queue[Any] = queue.Queue(maxsize=1)
    def read() -> None:
        try:
            row = stream.readline(maximum + 1)
            arrival.put(row)
        except Exception:
            arrival.put(None)
    threading.Thread(target=read, name="once-private-protocol-reader", daemon=True).start()
    try:
        line = arrival.get(timeout=timeout)
    except queue.Empty:
        raise OcrError("once_protocol_timeout") from None
    if not isinstance(line, str) or not line.endswith("\n") or len(line.encode("utf-8")) > maximum:
        raise OcrError("once_private_channel_closed_or_oversized")
    return line


def _once_emit(stream: Any, packet: Mapping[str, Any]) -> None:
    stream.write(json.dumps(dict(packet), ensure_ascii=True, separators=(",", ":")) + "\n")
    stream.flush()


def _once_validate_grant(grant: Mapping[str, Any], ready: Mapping[str, Any], attempt: _OnceAttempt,
                         parent: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    attempt.consume()  # Even malformed/cancelled/expired attempts are never retried.
    if not isinstance(grant, Mapping) or grant.get("schema") != ONCE_GRANT_SCHEMA or grant.get("type") != "confirm_once" or grant.get("action") != "read_once_and_local_mcp" or grant.get("authorization_granted") is not True:
        raise OcrError("explicit_live_ui_confirmation_required")
    for key, expected in (("challenge", attempt.challenge), ("backend_digest", ready["backend_digest"]),
                          ("scene_binding_sha256", ready["scene_binding_sha256"]), ("profile_sha256", ready["profile_sha256"])):
        if grant.get(key) != expected:
            raise OcrError("once_live_binding_mismatch")
    nonce = grant.get("nonce")
    if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", nonce):
        raise OcrError("once_nonce_invalid")
    created = _once_decimal_filetime(grant.get("created_filetime"))
    expires = _once_decimal_filetime(grant.get("expires_filetime"))
    now = _once_filetime_now()
    if not 0 < expires-created <= 15*10_000_000 or created > now + 1_000_000 or expires <= now or now-created > 15*10_000_000:
        raise OcrError("once_grant_expired_or_invalid")
    if grant.get("issuer_pid") != parent["ui"]["pid"] or grant.get("issuer_process_start_filetime") != parent["ui"]["start_filetime"]:
        raise OcrError("once_live_issuer_mismatch")
    snapshot = _once_window_snapshot(context["target"], context["roi"])
    top = snapshot["windows"]["toplevel"]["hwnd"]
    if _integer(grant.get("foreground_hwnd"), handle=True) != top or snapshot["foreground_hwnd"] != top or type(grant.get("last_input_tick")) is not int or grant["last_input_tick"] != snapshot["last_input_tick"]:
        raise OcrError("once_focus_or_input_epoch_changed")
    return {"nonce": nonce, "created_filetime": str(created), "expires_filetime": str(expires),
            "foreground_hwnd": top, "last_input_tick": snapshot["last_input_tick"]}


def _once_union_area(rects: list[list[int]]) -> int:
    xs=sorted({v for r in rects for v in (r[0],r[2])}); area=0
    for left,right in zip(xs,xs[1:]):
        intervals=sorted((r[1],r[3]) for r in rects if r[0]<=left and r[2]>=right)
        end=None; length=0
        for a,b in intervals:
            if end is None or a>end: length+=b-a;end=b
            elif b>end: length+=b-end;end=b
        area+=(right-left)*length
    return area


def _once_final_masks(candidate: Mapping[str, Any], roi: list[int]) -> list[list[int]]:
    gate=candidate["calibration_gate"]
    if gate.get("technical_complete") is True:
        absolute=[]
        for value in gate["known_strong_input_rects_screen"]:
            r=_rect(value);absolute.append([r[0]-2,r[1]-2,r[2]+2,r[3]+2])
    else:
        absolute=_once_manual_technical(candidate)
    result=[]
    for value in absolute:
        r=_once_intersection(value,roi)
        if r: result.append([r[0]-roi[0],r[1]-roi[1],r[2]-roi[0],r[3]-roi[1]])
    if _once_union_area(result)>=(roi[2]-roi[0])*(roi[3]-roi[1]):
        raise OcrError("once_no_unmasked_region")
    return result


def _once_qpc_100ns() -> str:
    import ctypes
    q,f=ctypes.c_longlong(),ctypes.c_longlong()
    kernel=ctypes.WinDLL("kernel32")
    if not kernel.QueryPerformanceCounter(ctypes.byref(q)) or not kernel.QueryPerformanceFrequency(ctypes.byref(f)):
        raise OcrError("once_clock_unavailable")
    return str(q.value*10_000_000//f.value)


def _once_epoch(context: Mapping[str, Any], parent: Mapping[str, Any], grant: Mapping[str, Any], revoke: int) -> None:
    _once_wait_event(revoke,signaled=False)
    if _once_filetime_now()>=int(grant["expires_filetime"]): raise OcrError("once_grant_expired")
    for name in ("ui","python"):
        live=_once_windows_process(parent[name]["pid"])
        if live!=parent[name]: raise OcrError("once_issuer_process_changed")
    snap=_once_window_snapshot(context["target"],context["roi"])
    if snap["foreground_hwnd"]!=grant["foreground_hwnd"] or snap["last_input_tick"]!=grant["last_input_tick"]:
        raise OcrError("once_input_or_focus_changed")


def _once_refresh(config: Any, args: Any, ready: Mapping[str, Any], parent: Mapping[str, Any], grant: Mapping[str, Any]) -> dict[str, Any]:
    new,context=_once_prepare(config,_option(args,"profile_path"),_option(args,"backend_binding_path"))
    if not new["ready"] or any(new[k]!=ready[k] for k in ("backend_digest","profile_sha256","scene_binding_sha256")):
        raise OcrError("once_scene_or_implementation_changed")
    _once_epoch(context,parent,grant,_option(args,"revocation_event_handle"))
    return context


def _once_native_request(context: Mapping[str, Any], parent: Mapping[str, Any], grant: Mapping[str, Any], args: Any, session: str) -> dict[str, Any]:
    ready=context["readiness"];target=context["target"];snap=context["snapshot"];wins=snap["windows"]
    masks=_once_final_masks(context["scene_candidate"],context["roi"])
    return {
        "schema":"dingtalkbridge.wgc-once-native-request.v1","policy":ONCE_POLICY,
        "authorization_granted":True,"manual_semantic_confirmation":True,"automatic_input_validation_complete":False,
        "technical_complete":True,"known_strong_input_geometry_complete":True,
        "nonce":grant["nonce"],"attempt_id":session,"backend_digest":ready["backend_digest"],
        "scene_binding_sha256":ready["scene_binding_sha256"],"profile_sha256":ready["profile_sha256"],
        "created_filetime":grant["created_filetime"],"expires_filetime":grant["expires_filetime"],
        "backend_binding_path":str(context["binding_path"]),"backend_binding_sha256":_once_sha(context["binding_path"]),
        "helper_sha256":context["build"]["file_hashes"]["producer_exe"],"ui":parent["ui"],"python":parent["python"],
        "target":{"pid":target["pid"],"start_filetime":context["process"]["start_filetime"],"session_id":target["session_id"],"file_sha256":target["file_sha256"],
                  "toplevel_hwnd":wins["toplevel"]["hwnd"],"semantic_hwnd":wins["semantic"]["hwnd"],"message_hwnd":wins["message"]["hwnd"],
                  "window_rect":wins["toplevel"]["bounds"],"dwm_rect":snap["dwm_bounds"],"semantic_rect":wins["semantic"]["bounds"],"message_rect":wins["message"]["bounds"],
                  "toplevel_class":wins["toplevel"]["class_name"],"semantic_class":wins["semantic"]["class_name"],"message_class":wins["message"]["class_name"]},
        "roi_screen":context["roi"],"capture_basis":context["capture_basis"],
        "masks_roi_pixels":masks,"mask_padding_contract":"final_roi_pixels_padding_applied_once_v1",
        "expected_masked_union_area":_once_union_area(masks),"proof_complete_qpc_100ns":_once_qpc_100ns(),
        "last_input_tick":grant["last_input_tick"],"foreground_hwnd":grant["foreground_hwnd"],
        "native_event_handle":_option(args,"native_event_handle"),"revocation_event_handle":_option(args,"revocation_event_handle")}


def _once_native_exchange(config: Any, args: Any, context: dict[str, Any], parent: Mapping[str, Any], grant: Mapping[str, Any], session: str, summary: dict[str, Any]) -> Mapping[str, Any]:
    """Private supervised pipe; only masked ROI text can leave the native helper."""
    request=_once_native_request(context,parent,grant,args,session)
    startup=subprocess.STARTUPINFO()
    startup.lpAttributeList={"handle_list":[request["native_event_handle"],request["revocation_event_handle"]]}
    process=None
    try:
        process=subprocess.Popen([str(_once_assets()["producer_exe"]),"--live-ui-once"],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                 text=True,encoding="utf-8",errors="strict",startupinfo=startup,close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW)
        _once_emit(process.stdin,request)
        rows: queue.Queue[Any]=queue.Queue(maxsize=4)
        def read_private() -> None:
            try:
                for _ in range(4):
                    line=process.stdout.readline(MAX_OUTPUT_BYTES+1)
                    if not line or len(line.encode("utf-8"))>MAX_OUTPUT_BYTES: break
                    rows.put(json.loads(line),timeout=1)
            except Exception: pass
            finally:
                try: rows.put(None,timeout=1)
                except queue.Full: pass
        threading.Thread(target=read_private,name="once-native-private-reader",daemon=True).start()
        permitted=False;until=time.monotonic()+15
        while time.monotonic()<until:
            _once_epoch(context,parent,grant,request["revocation_event_handle"])
            try: event=rows.get(timeout=0.05)
            except queue.Empty: continue
            if not isinstance(event,dict): raise OcrError("once_native_channel_closed")
            kind=event.get("type")
            if kind=="native_capture_started": summary["capture_started"]=True;continue
            if kind=="native_ready_for_map":
                if permitted or event.get("attempt_id")!=session or event.get("scene_binding_sha256")!=context["scene_hash"] or event.get("backend_digest")!=context["readiness"]["backend_digest"] or event.get("masked_pixels")!=request["expected_masked_union_area"] or event.get("width")!=context["roi"][2]-context["roi"][0] or event.get("height")!=context["roi"][3]-context["roi"][1]:
                    raise OcrError("once_native_roi_proof_mismatch")
                context=_once_refresh(config,args,context["readiness"],parent,grant)
                _once_emit(process.stdin,{"schema":"dingtalkbridge.wgc-once-permit-map.v1","type":"permit_map","permit_map":True,
                                         "attempt_id":session,"scene_binding_sha256":context["scene_hash"],"backend_digest":context["readiness"]["backend_digest"]})
                permitted=True;continue
            if kind=="native_result":
                summary["capture_started"] = summary["capture_started"] or event.get("capture_started") is True
                summary["pixels_read"] = event.get("pixels_read") is True
                if not permitted or event.get("status")!="available" or event.get("attempt_id")!=session or event.get("scene_binding_sha256")!=context["scene_hash"] or event.get("strong_inputs_masked_before_cpu") is not True:
                    raise OcrError("once_native_observation_unavailable")
                process.wait(timeout=2)
                if process.returncode!=0: raise OcrError("once_native_exit_failed")
                return event
            raise OcrError("once_native_protocol_invalid")
        raise OcrError("once_native_deadline")
    finally:
        if process is not None:
            try:
                if process.stdin: process.stdin.close()
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=1)
                    except subprocess.TimeoutExpired: process.kill();process.wait(timeout=1)
            finally:
                if process.stdout: process.stdout.close()


def _once_context_record(event: Mapping[str, Any], context: Mapping[str, Any], session: str) -> dict[str, Any]:
    text=event.get("text");language=event.get("language")
    if not isinstance(text,str) or not text.strip() or len(text.encode("utf-8"))>MAX_TEXT_BYTES or not isinstance(language,str) or not re.fullmatch(r"zh(?:-[A-Za-z0-9]{2,8}){1,3}",language,re.I):
        raise OcrError("once_ocr_observation_invalid")
    observed=parse_timestamp(event.get("observed_at"))
    if abs((datetime.now(timezone.utc)-observed).total_seconds())>30: raise OcrError("once_ocr_observation_stale")
    count=len([line for line in text.splitlines() if line.strip()]);words=len(text.split())
    conversation="ocr:manual-roi:"+session
    return {"id":"ocr-screen-"+hashlib.sha256((conversation+text).encode("utf-8")).hexdigest(),"conversation_id":conversation,
            "conversation_title":"User-confirmed visible DingTalk region (conversation unknown)","sender_id":"unknown","sender_name":"unknown",
            "text":text,"timestamp":event["observed_at"],"source":SOURCE,"confidence":0,
            "metadata":{"kind":"screen_context","is_message":False,"collector_session_id":session,"scope_generation":0,
                        "timestamp_kind":"observation_time","account_identity":"unknown","sender_identity":"unknown","conversation_identity":"unknown",
                        "coverage":COVERAGE,"history_available":False,"partial":True,"cropped":True,"sourceConfidence":0,"numeric_confidence_available":False,
                        "confidence_kind":"not_provided_by_windows_ocr","content_classification":"unverified_ocr_screen_context",
                        "desktop_kind":"interactive_default","evidence_region_counts":{"chat_component":count},
                        "ocr":{"engine":"Windows.Media.Ocr","language":language,"line_count":count,"word_count":words,"counts_method":"recognized_text_lines_and_whitespace_tokens"},
                        "capture":{"method":"wgc_verified_toplevel_gpu_roi_masked","policy":ONCE_POLICY,"manual_semantic_confirmation":True,
                                   "automatic_input_validation_complete":False,"strong_inputs_masked_before_cpu":True,"composed_scene":True,
                                   "coordinate_space":"physical_screen_pixels","scene_binding_sha256":context["scene_hash"],
                                   "crop_rect_screen":context["roi"],"source_hwnd":context["snapshot"]["windows"]["toplevel"]["hwnd"],
                                   "post_capture_geometry_verified":True,"backend_digest":context["readiness"]["backend_digest"]}}}


def run_user_confirmed_once(config: Any, store: Any, args: Any) -> dict[str, Any]:
    """Live UI only, one attempt. No offline grant loading or automatic consent."""
    session="ocr-once-"+uuid4().hex
    summary={"status":"rejected","reason":"not_authorized","source":SOURCE,"mode":ONCE_POLICY,"collector_session_id":session,
             "capture_started":False,"pixels_read":False,"ocr_started":False,"attempt_consumed":False,"message_count":0,"verified_message_count":0,
             "screen_contexts_added":0,"ingested_screen_contexts":0,"captured_text_items":0,"evidence_region_counts":{},"cropSafety":False,
             "running":False,"collector_running":False,"coverage":COVERAGE,"history_available":False,"desktop_kind":"interactive_default",
             "confidence":0,"numeric_confidence_available":False,"account_identity":"unknown","scope_generation":0,"graceful_stop":True,
             "database_path_hash":hashlib.sha256(str(Path(store.path).resolve()).casefold().encode("utf-8")).hexdigest()}
    attempt=_OnceAttempt()
    try:
        if Path(store.path).resolve()!=Path(config.database_path).resolve(): raise OcrError("once_database_mismatch")
        ready,context=_once_prepare(config,_option(args,"profile_path"),_option(args,"backend_binding_path"))
        if not ready["ready"]: summary["reason"]="technical_readiness_closed";return summary
        parent=_once_live_parent(context)
        _once_wait_event(_option(args,"revocation_event_handle"),signaled=False)
        # Challenge is emitted only on the private UI channel; never contains pixels/text.
        from datetime import timedelta
        _once_emit(sys.stdout,{"schema":"dingtalkbridge.roi-once-challenge.v1","type":"challenge","ready":True,
                               "challenge":attempt.challenge,"backend_digest":ready["backend_digest"],"profile_sha256":ready["profile_sha256"],
                               "scene_binding_sha256":ready["scene_binding_sha256"],"expires_utc":(datetime.now(timezone.utc)+timedelta(seconds=15)).isoformat()})
        raw=json.loads(_once_read_line(sys.stdin,15,64*1024))
        grant=_once_validate_grant(raw,ready,attempt,parent,context)
        summary["attempt_consumed"]=True
        _once_wait_event(_option(args,"click_event_handle"),signaled=True)
        context=_once_refresh(config,args,ready,parent,grant)
        event=_once_native_exchange(config,args,context,parent,grant,session,summary)
        summary["ocr_started"]=True
        context=_once_refresh(config,args,ready,parent,grant)
        record=_once_context_record(event,context,session)
        _once_epoch(context,parent,grant,_option(args,"revocation_event_handle"))
        added=bool(store.ingest(record))
        summary.update(status="completed",reason="one_shot_screen_context_stored" if added else "screen_context_filtered_or_duplicate",cropSafety=True,
                       screen_contexts_added=int(added),ingested_screen_contexts=int(added),captured_text_items=record["metadata"]["ocr"]["line_count"],
                       evidence_region_counts=record["metadata"]["evidence_region_counts"],last_observed_at=record["timestamp"])
    except KeyboardInterrupt:
        summary.update(status="cancelled",reason="once_cancelled")
    except Exception:
        summary.update(status="failed" if summary["capture_started"] else "rejected",reason="once_validation_or_capture_failed")
    finally:
        summary["attempt_consumed"]=attempt.spent
        summary["heartbeat_at"]=_stamp();summary["stopped_at"]=_stamp()
        _write_status(Path(config.data_dir),summary)
    return summary
