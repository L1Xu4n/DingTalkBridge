"""Discover a visible DingTalk top window on the current user's Default desktop."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from .native_controller import configured_target_path


def discover_targets() -> list[dict[str, int]]:
    """Enumerate only window/process metadata; never read titles or message text."""
    if os.name != "nt":
        raise ValueError("native collection requires Windows")
    expected_path = configured_target_path()
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenDesktopW.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    user.EnumDesktopWindows.argtypes = [wintypes.HANDLE, callback_type, wintypes.LPARAM]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    found: list[dict[str, int]] = []

    @callback_type
    def visit(hwnd: int, _parameter: int) -> bool:
        """Keep visible windows whose process image exactly matches this build target."""
        if not user.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        thread = user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = kernel.OpenProcess(0x1000, False, pid.value)
        if not process:
            return True
        try:
            path, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
            if not kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(size)):
                return True
            if os.path.basename(path.value).lower() != "dingtalk.exe":
                return True
            if expected_path and os.path.normcase(path.value) != os.path.normcase(expected_path):
                return True
            rect = wintypes.RECT()
            if user.GetWindowRect(hwnd, ctypes.byref(rect)):
                area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
                if area:
                    found.append({"pid": pid.value, "hwnd": int(hwnd), "thread_id": thread, "area": area})
        finally:
            kernel.CloseHandle(process)
        return True

    desktop = user.OpenDesktopW("Default", 0, False, 0x0041)
    if not desktop:
        raise ValueError("Default desktop is unavailable")
    try:
        if not user.EnumDesktopWindows(desktop, visit, 0):
            raise ValueError("Default desktop enumeration failed")
    finally:
        user.CloseDesktop(desktop)
    return sorted(found, key=lambda row: row["area"], reverse=True)


def select_target(pid: int | None = None, hwnd: int | None = None) -> dict[str, int]:
    """Select the largest window in one process; require a PID if several clients exist."""
    candidates = [row for row in discover_targets()
                  if (pid is None or row["pid"] == pid) and (hwnd is None or row["hwnd"] == hwnd)]
    if not candidates:
        raise ValueError("no visible DingTalk window on Default desktop")
    if len({row["pid"] for row in candidates}) > 1:
        raise ValueError("multiple DingTalk processes have visible windows; select --pid")
    return candidates[0]
