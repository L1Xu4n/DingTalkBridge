"""List account-bound group storage and resolve its files through the attachment readers."""

from dataclasses import asdict
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import tempfile
import time
import uuid
from typing import Any

from .attachment_cache import cache_lock
from .attachments import Attachment, AttachmentError, _directory, _object, _positive_int, _text
from .store import Store


def _conversation(store: Store, conversation_id: str) -> Any:
    """Recheck archive policy and require a native conversation belonging to the bound account."""
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise AttachmentError("invalid_conversation_id")
    clauses, arguments = store._where()
    row = store.connection.execute(
        "SELECT m.* FROM messages m WHERE m.conversation_id=? AND m.source='windows_native_history' AND "
        + " AND ".join(clauses) + " ORDER BY m.seq DESC LIMIT 1", (conversation_id, *arguments),
    ).fetchone()
    if row is None:
        raise AttachmentError("group_files_conversation_not_available")
    account = store.native_account_id()
    if not account or _text(_object(row["metadata"]).get("account_id")) != account:
        raise AttachmentError("group_files_account_not_available")
    return row


def _write_json(path: Path, value: Any) -> None:
    """Publish private entry and query metadata atomically across MCP processes."""
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=True)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _rpc(store: Store, operation: str, payload: bytes) -> Any:
    """Submit a bounded native read, reuse pending requests and cache completed metadata briefly."""
    try:
        import msgpack
    except ImportError:
        raise AttachmentError("group_files_missing_install_dingtalkbridge_attachments_extra") from None
    account = store.native_account_id()
    identity = json.dumps([str(store.path.resolve()), account, operation, payload.hex()]).encode()
    directory = store.config.data_dir / "attachments" / hashlib.sha256(identity).hexdigest()
    requests = store.config.data_dir / "native-attachment-requests"
    requests.mkdir(parents=True, exist_ok=True)
    with cache_lock(directory):
        directory.mkdir(parents=True, exist_ok=True)
        os.utime(directory, None)
        cached = directory / "response.msgpack"
        if not cached.exists() or time.time() - cached.stat().st_mtime >= 15:
            if not store._collector_status().get("running"):
                raise AttachmentError("attachment_collector_not_running_reconnect_MCP")
            state = directory / "request.json"
            try:
                job = json.loads(state.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                job = {}
            if not re.fullmatch(r"[a-f0-9]{32}", str(job.get("key", ""))) or time.time() - job.get("created", 0) >= 65:
                job = {"key": uuid.uuid4().hex, "created": time.time()}
                _write_json(state, job)
            pending = requests / (job["key"] + ".request")
            result, failed = pending.with_suffix(".result"), pending.with_suffix(".error")
            if failed.exists():
                state.unlink(missing_ok=True)
                raise AttachmentError("group_files_native_request_failed_check_native_version_and_access")
            if not result.exists() and not pending.exists() and not pending.with_suffix(".started").exists():
                text = account + "\n" + operation + "\n" + payload.hex() + "\n"
                if len(text.encode()) > 32768:
                    raise AttachmentError("group_files_request_too_large")
                descriptor, temporary = tempfile.mkstemp(prefix=".request-", dir=requests)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                    output.write(text)
                os.replace(temporary, pending)
            deadline = time.monotonic() + 8
            while not result.exists():
                if failed.exists():
                    state.unlink(missing_ok=True)
                    raise AttachmentError("group_files_native_request_failed_check_native_version_and_access")
                if time.monotonic() >= deadline:
                    raise AttachmentError("group_files_query_pending_retry_same_request")
                time.sleep(.1)
            if result.stat().st_size > 4 * 1024 * 1024:
                raise AttachmentError("group_files_response_too_large")
            os.replace(result, cached)
            pending.with_suffix(".started").unlink(missing_ok=True)
            state.unlink(missing_ok=True)
        try:
            return msgpack.unpackb(cached.read_bytes(), raw=False, strict_map_key=False,
                                   max_str_len=4 * 1024 * 1024, max_array_len=10000, max_map_len=10000)
        except (ValueError, TypeError):
            raise AttachmentError("group_files_response_layout_unsupported") from None


def resolve_entry(store: Store, attachment_id: str) -> Attachment:
    """Resolve only previously listed opaque drive IDs and reapply account/conversation checks."""
    if not re.fullmatch(r"drive-[a-f0-9]{64}", attachment_id):
        raise AttachmentError("invalid_attachment_id")
    key = hashlib.sha256((str(store.path.resolve()) + attachment_id).encode()).hexdigest()
    path = store.config.data_dir / "attachments" / key / "entry.json"
    try:
        entry = Attachment(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        raise AttachmentError("group_file_not_available_list_again") from None
    if entry.id != attachment_id or entry.account_id != store.native_account_id():
        raise AttachmentError("group_files_account_not_available")
    _conversation(store, entry.conversation_id)
    return entry


def list_group_files(store: Store, conversation_id: str, *, folder_id: str | None = None,
                     cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List the group root or one returned folder, preserving the server's continuation cursor."""
    from .attachments import resolve
    try:
        import msgpack
    except ImportError:
        raise AttachmentError("group_files_missing_install_dingtalkbridge_attachments_extra") from None
    _conversation(store, conversation_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= store.config.max_tool_results:
        raise AttachmentError("invalid_group_files_limit")
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 256):
        raise AttachmentError("invalid_group_files_cursor")
    account = store.native_account_id()
    if folder_id:
        folder = resolve(store, folder_id)
        if not folder.is_directory or folder.conversation_id != conversation_id or folder.account_id != account:
            raise AttachmentError("group_files_folder_not_available")
        space, parent = folder.space_id, folder.file_id
    else:
        packer = msgpack.Packer(use_bin_type=True)
        # CSpace's group space is type 2; IM attachment and personal spaces use other types.
        space = _rpc(store, "group-space", packer.pack(conversation_id) + packer.pack(2))
        if isinstance(space, bool) or not isinstance(space, int) or space <= 0:
            raise AttachmentError("group_files_space_not_available")
        space, parent = str(space), "0"
    query = {4: space, 5: parent, 2: limit}
    if cursor is not None:
        query[1] = cursor
    response = _rpc(store, "list-directory", msgpack.packb(query, use_bin_type=True))
    if not isinstance(response, dict) or not isinstance(response.get(1), int):
        raise AttachmentError("group_files_response_layout_unsupported")
    if response[1] != 0:
        raise AttachmentError("group_files_client_error_" + str(response[1]))
    result = response.get(3)
    model = result.get(1) if isinstance(result, dict) else None
    if not isinstance(model, dict) or not isinstance(model.get(3), list) or not isinstance(model.get(5), bool):
        raise AttachmentError("group_files_response_layout_unsupported")
    items = []
    for source in model[3]:
        if not isinstance(source, dict) or not _text(source.get(1)) or not _text(source.get(4)):
            raise AttachmentError("group_files_entry_layout_unsupported")
        file_id = source[1]
        identity = json.dumps([account, conversation_id, space, file_id], ensure_ascii=True).encode()
        identifier = "drive-" + hashlib.sha256(identity).hexdigest()
        name = Path(source[4].replace("\\", "/")).name
        if name in {"", ".", ".."}:
            raise AttachmentError("group_files_entry_name_unavailable")
        is_directory = source.get(5) == "folder"
        entry = Attachment(
            id=identifier, message_id="", name=name,
            mime_type="inode/directory" if is_directory else mimetypes.guess_type(name)[0] or "application/octet-stream",
            size=_positive_int(source.get(2)), local_path="", url="", media_id="", file_id=file_id,
            space_id=space, conversation_id=conversation_id, native_message_id="0", account_id=account,
            native_path=_text(source.get(3)), timestamp_milliseconds=0, is_directory=is_directory,
            revision=json.dumps([_positive_int(source.get(10)), _text(source.get(91))]),
        )
        directory = _directory(store, entry)
        with cache_lock(directory):
            directory.mkdir(parents=True, exist_ok=True)
            _write_json(directory / "entry.json", asdict(entry))
        item = entry.public()
        item.update(availability="directory" if is_directory else "download_required",
                    download_on_read=not is_directory)
        if is_directory:
            item["list_on_read"] = True
        items.append(item)
    next_cursor = _text(model.get(1)) if model[5] else None
    if model[5] and (not next_cursor or next_cursor == cursor):
        raise AttachmentError("group_files_cursor_did_not_advance")
    return {"conversation_id": conversation_id, "folder_id": folder_id, "files": items,
            "total": _positive_int(model.get(2)), "has_more": model[5], "next_cursor": next_cursor}
