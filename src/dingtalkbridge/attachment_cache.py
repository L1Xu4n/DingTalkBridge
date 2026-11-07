"""Expire private attachment caches while coordinating readers across MCP processes."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
from typing import Any, Iterator

from .config import Config

CLEANUP_INTERVAL_SECONDS = 3600


def _try_lock(handle: Any) -> bool:
    """Acquire one nonblocking OS byte/file lock, returning false only for contention."""
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise
    return True


@contextmanager
def cache_lock(directory: Path, *, wait: bool = True) -> Iterator[bool]:
    """Keep a cache entry in use until its reader exits, or let cleanup skip a busy entry."""
    locks = directory.parent / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    if locks.resolve().parent != directory.parent.resolve():
        raise OSError("attachment cache lock directory is unavailable")
    with (locks / (directory.name + ".lock")).open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        while not _try_lock(handle):
            if not wait:
                yield False
                return
            time.sleep(0.05)
        # Closing the handle also releases its OS lock, including after an exception.
        yield True


def _last_used(directory: Path) -> float | None:
    """Inspect legacy and current cache timestamps without following links or junctions."""
    latest = 0.0
    pending = [directory]
    while pending:
        path = pending.pop()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return None
        latest = max(latest, info.st_mtime)
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
    return latest


def cleanup_cache(config: Config) -> int:
    """Remove expired owned attachment directories, preserving busy or recently written entries."""
    if config.attachment_cache_retention_days == 0:
        return 0
    root = config.data_dir.resolve() / "attachments"
    try:
        if root.resolve() != root:
            return 0
        directories = list(root.iterdir())
    except OSError:
        return 0
    cutoff = time.time() - config.attachment_cache_retention_days * 86400
    removed = 0
    for directory in directories:
        if not re.fullmatch(r"[a-f0-9]{64}", directory.name):
            continue
        try:
            with cache_lock(directory, wait=False) as acquired:
                if not acquired or not directory.is_dir() or directory.resolve().parent != root:
                    continue
                latest = _last_used(directory)
                if latest is None or latest > cutoff:
                    continue
                # The resolved target is an owned direct child, and readers hold the same lock.
                shutil.rmtree(directory)
                removed += 1
        except OSError:
            continue
    return removed


@contextmanager
def maintain_cache(config: Config) -> Iterator[None]:
    """Clean at MCP startup and hourly during idle time, then stop with the server."""
    if config.attachment_cache_retention_days == 0:
        yield
        return
    cleanup_cache(config)
    stopped = threading.Event()

    def maintain() -> None:
        """Wait between cleanup passes so idle MCP servers also expire cached attachments."""
        while not stopped.wait(CLEANUP_INTERVAL_SECONDS):
            cleanup_cache(config)

    worker = threading.Thread(target=maintain, name="attachment-cache-cleanup", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=1)
