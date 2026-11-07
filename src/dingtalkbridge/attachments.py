"""Resolve message-bound attachments and cache their actual bytes for document readers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any
from urllib import error, parse, request

from .store import Store
from .attachment_cache import cache_lock

MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MEDIA_HOSTS = {"down.dingtalk.com", "down-cdn.dingtalk.com", "static.dingtalk.com"}


class AttachmentError(ValueError):
    """A fixed, safe diagnostic which contains no temporary URL or authorization values."""


def _object(value: Any) -> dict[str, Any]:
    """Decode the nested JSON objects used by the native attachment representation."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return {}
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    """Keep native scalar strings without converting arbitrary objects to file paths."""
    return value if isinstance(value, str) else ""


def _positive_int(value: Any) -> int:
    """Normalize optional native byte counts and image dimensions without failing the catalog."""
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def _digest(path: Path) -> str:
    """Hash a cached file in bounded chunks on every supported Python version."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Attachment:
    """Private source locators plus public metadata for one message attachment."""

    id: str
    message_id: str
    name: str
    mime_type: str
    size: int
    local_path: str
    url: str
    media_id: str
    file_id: str
    space_id: str
    conversation_id: str
    native_message_id: str
    account_id: str
    native_path: str
    timestamp_milliseconds: int
    width: int = 0
    height: int = 0

    def public(self) -> dict[str, Any]:
        """Return metadata suitable for the model, excluding download credentials and paths."""
        result = {"attachment_id": self.id, "message_id": self.message_id, "name": self.name,
                  "mime_type": self.mime_type, "size": self.size or None}
        if self.width and self.height:
            result["expected_dimensions"] = [self.width, self.height]
        return result

    def fingerprint(self) -> str:
        """Invalidate cached content when the message's attachment identity changes."""
        identity = [self.media_id, self.file_id, self.space_id, self.name, self.size, self.local_path]
        if self.width and self.height:
            identity.extend([self.width, self.height])
        return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()


def _message(store: Store, *, message_id: str | None = None, sequence: int | None = None) -> Any:
    """Reapply the current archive policy every time an attachment is listed or read."""
    clauses, parameters = store._where()
    field, value = ("m.id", message_id) if message_id is not None else ("m.seq", sequence)
    row = store.connection.execute(
        "SELECT m.* FROM messages m WHERE " + field + "=? AND " + " AND ".join(clauses),
        (value, *parameters),
    ).fetchone()
    if row is None:
        raise AttachmentError("attachment_message_not_available")
    return row


def _catalog(row: Any) -> list[Attachment]:
    """Decode image and file locators, including JSON nested inside extension fields."""
    if row["source"] != "windows_native_history":
        return []
    body = _object(row["text"])
    metadata = _object(row["metadata"])
    candidates = []
    if metadata.get("content_type") == "2" and (body.get("mediaId") or body.get("url")):
        candidates.append((body, body))
    for item in body.get("attachments", []) if isinstance(body.get("attachments"), list) else []:
        if not isinstance(item, dict):
            continue
        extra = _object(item.get("extension"))
        # Link cards also use attachments; only media or actual file identifiers qualify.
        if extra.get("f_id") or extra.get("mediaId") or item.get("mediaId") or item.get("filepath"):
            candidates.append((item, extra))
    result = []
    for index, (item, extra) in enumerate(candidates):
        name = _text(extra.get("f_name")) or _text(item.get("filename"))
        url = _text(item.get("url"))
        suffix = Path(parse.urlsplit(url).path).suffix if url.startswith("https://") else ""
        name = Path(name.replace("\\", "/")).name or f"attachment{suffix or '.bin'}"
        if len(name) > 240:
            name = name[:200] + Path(name).suffix[:20]
        size = extra.get("f_size") or item.get("picSize") or item.get("size") or 0
        size = _positive_int(size)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        result.append(Attachment(
            id=f"att-{row['seq']}-{index}", message_id=row["id"], name=name, mime_type=mime, size=size,
            local_path=_text(item.get("filepath")), url=url,
            media_id=_text(extra.get("mediaId")) or _text(item.get("mediaId")),
            file_id=_text(extra.get("f_id")), space_id=_text(extra.get("s_id")),
            conversation_id=row["conversation_id"], native_message_id=_text(metadata.get("native_message_id")),
            account_id=_text(metadata.get("account_id")), native_path=_text(extra.get("path")),
            timestamp_milliseconds=int(row["timestamp_epoch"] * 1000),
            width=_positive_int(item.get("width")), height=_positive_int(item.get("height")),
        ))
    return result


def resolve(store: Store, attachment_id: str) -> Attachment:
    """Resolve an opaque archive attachment ID without accepting an arbitrary file or URL."""
    match = re.fullmatch(r"att-([1-9][0-9]{0,18})-([0-9]{1,5})", attachment_id)
    if not match:
        raise AttachmentError("invalid_attachment_id")
    attachments = _catalog(_message(store, sequence=int(match[1])))
    index = int(match[2])
    if index >= len(attachments):
        raise AttachmentError("attachment_not_available")
    return attachments[index]


def _directory(store: Store, attachment: Attachment) -> Path:
    """Keep cached bytes inside the current archive's private data directory."""
    key = hashlib.sha256((str(store.path.resolve()) + attachment.id).encode()).hexdigest()
    return store.config.data_dir / "attachments" / key


def _local_file(store: Store, attachment: Attachment) -> Path | None:
    """Find a source only within configured download directories and verify name and size."""
    roots = [Path(value).resolve() for value in store.config.attachment_dirs]
    candidates = [Path(attachment.local_path)] if attachment.local_path else []
    candidates.extend(root / attachment.name for root in roots)
    for candidate in candidates:
        try:
            path = candidate.resolve()
            if not any(path.is_relative_to(root) for root in roots):
                continue
            if path.name != attachment.name or not path.is_file():
                continue
            if attachment.size and path.stat().st_size != attachment.size:
                continue
            if not _image_complete(path, attachment):
                continue
            return path
        except OSError:
            continue
    return None


def _image_complete(path: Path, attachment: Attachment) -> bool:
    """Reject previews smaller than the archived image, allowing EXIF width/height rotation."""
    if not attachment.mime_type.startswith("image/") or not (attachment.width and attachment.height):
        return True
    from PIL import Image
    try:
        with Image.open(path) as source:
            actual = sorted(source.size)
        expected = sorted((attachment.width, attachment.height))
        return actual[0] >= expected[0] and actual[1] >= expected[1]
    except (OSError, ValueError):
        return False


def _safe_url(url: str) -> str:
    """Limit automatic downloads and redirects to observed DingTalk media HTTPS hosts."""
    parsed = parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in MEDIA_HOSTS or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise AttachmentError("attachment_download_url_unavailable")
    return url


class _Redirect(request.HTTPRedirectHandler):
    """Validate redirect destinations before following a media download."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        """Keep redirects within the same approved media origin family."""
        return super().redirect_request(req, fp, code, msg, headers, _safe_url(newurl))


def _download(url: str, destination: Path, *, timeout: int = 120) -> None:
    """Stream media with the configured network I/O timeout and fixed retrieval failures."""
    try:
        opener = request.build_opener(_Redirect())
        with opener.open(request.Request(_safe_url(url), headers={"User-Agent": "DingTalkBridge"}), timeout=timeout) as source:
            if "text/html" in source.headers.get("Content-Type", ""):
                raise AttachmentError("attachment_download_returned_html")
            copied = 0
            with destination.open("wb") as target:
                while block := source.read(1024 * 1024):
                    copied += len(block)
                    if copied > MAX_DOWNLOAD_BYTES:
                        raise AttachmentError("attachment_exceeds_download_size")
                    target.write(block)
    except error.HTTPError as exc:
        code = "attachment_requires_client_download" if exc.code in {401, 403} else "attachment_link_expired" if exc.code in {404, 410} else "attachment_download_failed"
        raise AttachmentError(code) from None
    except (OSError, error.URLError):
        raise AttachmentError("attachment_download_failed") from None


def _download_complete(path: Path) -> bool:
    """Wait for the client to close its destination before consuming downloaded bytes."""
    try:
        if not path.is_file() or not path.stat().st_size:
            return False
        if os.name != "nt":
            return True
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        kernel.CreateFileW.restype = wintypes.HANDLE
        handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0, None)
        if handle == wintypes.HANDLE(-1).value:
            return False
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle(handle)
        return True
    except OSError:
        return False


def _client_download(store: Store, attachment: Attachment, destination: Path) -> None:
    """Wait the configured time for a client download, retaining its request for continuation."""
    account = store.native_account_id()
    if not (attachment.media_id or attachment.file_id and attachment.space_id) or not account or account != attachment.account_id:
        raise AttachmentError("attachment_requires_client_download")
    if not re.fullmatch(r"[1-9][0-9]{0,18}", attachment.native_message_id):
        raise AttachmentError("attachment_native_message_id_unavailable")
    incoming = _directory(store, attachment) / ("incoming-" + attachment.fingerprint()[:16])
    incoming.mkdir(parents=True, exist_ok=True)
    downloaded = incoming / attachment.name
    requests = store.config.data_dir / "native-attachment-requests"
    requests.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((attachment.id + attachment.fingerprint()).encode()).hexdigest()
    pending = requests / (key + ".request")
    started, failed = pending.with_suffix(".started"), pending.with_suffix(".error")
    timeout = store.config.attachment_download_timeout_seconds
    if not _download_complete(downloaded):
        collector = store._collector_status()
        if not collector.get("running"):
            raise AttachmentError("attachment_collector_not_running_reconnect_MCP")
        if not pending.exists() and (not started.exists() or time.time() - started.stat().st_mtime > max(120, timeout * 2)):
            failed.unlink(missing_ok=True)
            arguments = {"cid_": attachment.conversation_id, "mId_": attachment.native_message_id,
                         "f_name_": attachment.name, "f_id_": attachment.file_id,
                         "f_space_id_": attachment.space_id, "f_path_": attachment.native_path,
                         "dest_path_": str(incoming.resolve())}
            payload = account + "\n" + json.dumps([json.dumps(arguments, ensure_ascii=True)], ensure_ascii=True) + "\n"
            if attachment.media_id and not attachment.file_id:
                payload = "\n".join((account, "media", attachment.conversation_id, attachment.native_message_id,
                                       str(downloaded.resolve()), str(attachment.timestamp_milliseconds), ""))
            if len(payload.encode()) > 32768:
                raise AttachmentError("attachment_download_request_too_large")
            descriptor, temporary = tempfile.mkstemp(prefix=".request-", dir=requests)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
                target.write(payload)
            os.replace(temporary, pending)
        deadline = time.monotonic() + timeout
        while not _download_complete(downloaded):
            if failed.exists():
                raise AttachmentError("attachment_native_download_binding_unavailable")
            if time.monotonic() >= deadline:
                raise AttachmentError("attachment_download_pending_retry_read_attachment")
            time.sleep(0.25)
    if not _image_complete(downloaded, attachment):
        downloaded.unlink(missing_ok=True)
        started.unlink(missing_ok=True)
        raise AttachmentError("attachment_image_preview_only_retry_read_attachment")
    shutil.copyfile(downloaded, destination)


def cached_file(store: Store, attachment: Attachment) -> Path | None:
    """Reuse a complete cache entry whose message identity and byte hash still match."""
    directory = _directory(store, attachment)
    manifest = directory / "manifest.json"
    try:
        values = json.loads(manifest.read_text(encoding="utf-8"))
        digest = values.get("sha256", "")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            return None
        path = directory / digest
        if values.get("fingerprint") == attachment.fingerprint() and digest == _digest(path) and _image_complete(path, attachment):
            return path
    except (OSError, ValueError):
        pass
    return None


def fetch(store: Store, attachment: Attachment) -> Path:
    """Cache complete local or downloaded bytes atomically, reusing them on later reads."""
    existing = cached_file(store, attachment)
    if existing:
        return existing
    directory = _directory(store, attachment)
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".fetch-", dir=directory)
    os.close(descriptor)
    staging = Path(temporary)
    try:
        local = _local_file(store, attachment)
        if local:
            if local.stat().st_size > MAX_DOWNLOAD_BYTES:
                raise AttachmentError("attachment_exceeds_download_size")
            shutil.copyfile(local, staging)
        elif attachment.url:
            try:
                _download(attachment.url, staging, timeout=store.config.attachment_download_timeout_seconds)
                if not _image_complete(staging, attachment):
                    raise AttachmentError("attachment_image_preview_only_retry_read_attachment")
            except AttachmentError as exc:
                if str(exc) not in {"attachment_requires_client_download", "attachment_link_expired",
                                   "attachment_download_url_unavailable", "attachment_image_preview_only_retry_read_attachment"}:
                    raise
                _client_download(store, attachment, staging)
        else:
            _client_download(store, attachment, staging)
        if not staging.stat().st_size:
            raise AttachmentError("attachment_content_empty")
        if not _image_complete(staging, attachment):
            raise AttachmentError("attachment_image_preview_only_retry_read_attachment")
        digest = _digest(staging)
        content_path = directory / digest
        if not content_path.exists():
            os.replace(staging, content_path)
        manifest = {"fingerprint": attachment.fingerprint(), "sha256": digest}
        staging.write_text(json.dumps(manifest), encoding="utf-8")
        os.replace(staging, directory / "manifest.json")
        return content_path
    except OSError:
        raise AttachmentError("attachment_cache_failed") from None
    finally:
        staging.unlink(missing_ok=True)


def list_attachments(store: Store, message_id: str) -> dict[str, Any]:
    """List actual message attachments without downloading them or exposing their locators."""
    if not isinstance(message_id, str) or not message_id.strip():
        raise AttachmentError("invalid_message_id")
    items = []
    for attachment in _catalog(_message(store, message_id=message_id)):
        item = attachment.public()
        item["availability"] = "cached" if cached_file(store, attachment) else "local" if _local_file(store, attachment) else "download_required"
        item["download_on_read"] = item["availability"] == "download_required"
        items.append(item)
    return {"message_id": message_id, "attachments": items}


def read_attachment(store: Store, attachment_id: str, **options: Any) -> dict[str, Any]:
    """Hold the cache during download/reading and refresh its retention timestamp on use."""
    from .attachment_readers import read
    attachment = resolve(store, attachment_id)
    directory = _directory(store, attachment)
    try:
        with cache_lock(directory):
            directory.mkdir(parents=True, exist_ok=True)
            os.utime(directory, None)
            path = fetch(store, attachment)
            try:
                details, media = read(path, attachment.name, attachment.mime_type, **options)
            except ImportError:
                raise AttachmentError("attachment_readers_missing_install_dingtalkbridge_attachments_extra") from None
            except AttachmentError:
                raise
            except Exception:
                raise AttachmentError("attachment_could_not_be_parsed") from None
            metadata = {**attachment.public(), "bytes": path.stat().st_size, "sha256": _digest(path), **details}
            os.utime(directory, None)
            for block in media:
                if block.get("type") == "resource":
                    block["resource"]["uri"] = "attachment://" + attachment.id
    except OSError:
        raise AttachmentError("attachment_cache_failed") from None
    return {"isError": False, "structuredContent": metadata,
            "content": [{"type": "text", "text": json.dumps(metadata, ensure_ascii=False)}, *media]}
