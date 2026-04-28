"""Window management tools: resize, switch, launch_app.

All OS-specific operations go through an injectable `_backend` so tests
can validate behavior without touching real windows.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from typing import Any, Protocol

from ..errors import ComputerMCPError, ErrorCategory

logger = logging.getLogger(__name__)


class WindowBackend(Protocol):
    def switch(self, title: str) -> bool: ...
    def resize(self, title: str, x: int, y: int, w: int, h: int) -> bool: ...
    def minimize(self, title: str) -> bool: ...
    def maximize(self, title: str) -> bool: ...
    def launch(self, app_name: str) -> bool: ...
    def snapshot_hwnds(self) -> set[int]: ...
    def probe_launched_window(
        self, timeout_s: float = 3.0, before: set[int] | None = None,
    ) -> tuple[str | None, int | None]: ...


# Title substrings that identify windows belonging to the agent / dev tooling
# (and are therefore NEVER the window a user just launched).
_DEV_WINDOW_PATTERNS: tuple[str, ...] = (
    "gc-cli",
    "gc-computer",
    "claude code",
    "anthropic",
    "windows powershell",
    "powershell 7",
    "pwsh",
    "cmd.exe",
    "command prompt",
    "windows terminal",
    "conhost",
)


def _is_dev_window(title: str) -> bool:
    """Return True if the window title looks like an agent/terminal/dev window
    (one that should be skipped when diffing HWNDs to find a just-launched app).
    Empty titles are treated as dev — they don't help the caller regardless.
    """
    if not title:
        return True
    lowered = title.lower()
    return any(p in lowered for p in _DEV_WINDOW_PATTERNS)


def _enum_top_level_hwnds() -> set[int]:
    """Enumerate visible top-level HWNDs. Empty set on non-Windows or failure."""
    if sys.platform != "win32":
        return set()
    import ctypes

    user32 = ctypes.windll.user32
    EnumWindows = user32.EnumWindows
    IsWindowVisible = user32.IsWindowVisible

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
    )
    hwnds: set[int] = set()

    def _cb(hwnd, _lparam):
        if IsWindowVisible(hwnd):
            hwnds.add(int(hwnd))
        return True

    try:
        EnumWindows(EnumWindowsProc(_cb), 0)
    except OSError as e:
        logger.debug("EnumWindows failed: %s", e)
    return hwnds


def _get_window_title(hwnd: int) -> str:
    """Return a window's title by HWND, or '' if unavailable."""
    if sys.platform != "win32" or not hwnd:
        return ""
    import ctypes

    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if not length:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value or ""


# Module-level handle to user32.dll. Cached for switch() so tests can
# monkeypatch _user32 with a fake that records calls without touching
# real Win32. None on non-Windows so import doesn't crash on CI.
if sys.platform == "win32":
    import ctypes as _ctypes

    _user32 = _ctypes.windll.user32
else:
    _user32 = None


# Known app name → executable mapping. Kept intentionally small; extend as needed.
# Uses cmd /c start for Office apps so Office URI protocol handles the launch
# regardless of system locale (unlike Windows-MCP's upstream launch_app which
# hardcoded English names).
_APP_COMMAND: dict[str, list[str]] = {
    "notepad": ["notepad.exe"],
    "calc": ["calc.exe"],
    "calculator": ["calc.exe"],
    "excel": ["cmd", "/c", "start", "excel"],
    "word": ["cmd", "/c", "start", "winword"],
    "outlook": ["cmd", "/c", "start", "outlook"],
    "powerpoint": ["cmd", "/c", "start", "powerpnt"],
    "cmd": ["cmd.exe"],
    "explorer": ["explorer.exe"],
}


class _DefaultBackend:
    def switch(self, title: str) -> bool:
        """Activate a top-level window whose title contains `title`.

        Uses EnumWindows (the same source as scope='desktop' UIA walks) +
        SetForegroundWindow, NOT pygetwindow — the latter sometimes misses
        windows that are present in UIA, observed with Telegram Desktop.

        Restores minimized windows first (ShowWindow SW_RESTORE) before
        bringing them to foreground. Dev windows (gc-cli, our own terminal)
        are skipped so we never activate the host process by accident.
        """
        if sys.platform != "win32" or _user32 is None:
            return False
        needle = (title or "").strip().lower()
        if not needle:
            return False
        for hwnd in _enum_top_level_hwnds():
            win_title = _get_window_title(hwnd)
            if not win_title or _is_dev_window(win_title):
                continue
            if needle in win_title.lower():
                SW_RESTORE = 9
                _user32.ShowWindow(hwnd, SW_RESTORE)
                _user32.SetForegroundWindow(hwnd)
                return True
        return False

    def resize(self, title: str, x: int, y: int, w: int, h: int) -> bool:
        import pygetwindow as gw  # type: ignore

        matches = [w for w in gw.getAllWindows() if title.lower() in w.title.lower()]
        if not matches:
            return False
        matches[0].resizeTo(w, h)
        matches[0].moveTo(x, y)
        return True

    def minimize(self, title: str) -> bool:
        import pygetwindow as gw  # type: ignore

        matches = [w for w in gw.getAllWindows() if title.lower() in w.title.lower()]
        if not matches:
            return False
        matches[0].minimize()
        return True

    def maximize(self, title: str) -> bool:
        import pygetwindow as gw  # type: ignore

        matches = [w for w in gw.getAllWindows() if title.lower() in w.title.lower()]
        if not matches:
            return False
        matches[0].maximize()
        return True

    def snapshot_hwnds(self) -> set[int]:
        return _enum_top_level_hwnds()

    def probe_launched_window(
        self, timeout_s: float = 3.0, before: set[int] | None = None,
    ) -> tuple[str | None, int | None]:
        """Detect the window that was just launched by HWND-diff.

        Polls for up to timeout_s, returning the first NEW top-level HWND
        (not present in `before`) whose title passes the dev-window filter.
        Returns (None, None) if no such window appears. Using HWND-diff
        instead of GetForegroundWindow avoids returning the agent's own
        terminal if focus flips back to it before the launched app renders.
        """
        if sys.platform != "win32":
            return (None, None)
        import time as _t

        snapshot = before if before is not None else _enum_top_level_hwnds()
        end = _t.time() + timeout_s
        while _t.time() < end:
            new_hwnds = _enum_top_level_hwnds() - snapshot
            for hwnd in new_hwnds:
                title = _get_window_title(hwnd)
                if _is_dev_window(title):
                    continue
                return (title, hwnd)
            _t.sleep(0.2)
        return (None, None)

    def launch(self, app_name: str) -> bool:
        if sys.platform != "win32":
            return False
        key = app_name.strip().lower()

        # 1. Fast path — known aliases for common Office + shell apps.
        cmd = _APP_COMMAND.get(key)
        if cmd is not None:
            return _spawn(cmd)

        # 2. Executable resolvable via PATH.
        if shutil.which(app_name):
            return _spawn([app_name])

        # 3. Start Menu / UWP lookup by friendly name (Get-StartApps).
        app_id = _lookup_start_menu(app_name)
        if app_id:
            return _spawn_uwp(app_id)

        return False


def _spawn(cmd: list[str]) -> bool:
    try:
        subprocess.Popen(cmd, shell=False)
        return True
    except OSError as e:  # noqa: BLE001
        logger.debug("launch spawn failed for %r: %s", cmd, e)
        return False


def _spawn_uwp(app_id: str) -> bool:
    """Launch a UWP / Start-Menu-registered app via PowerShell Start-Process.

    Empirically (2026-04-21) direct paths from a Python child process do NOT
    trigger UWP activation: neither subprocess.Popen(["explorer.exe",
    "shell:AppsFolder\\<AppID>"]) nor ShellExecuteW starts Jazz / Paint / the
    new Notepad. Start-Process from PowerShell triggers activation reliably.
    """
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return False
    try:
        subprocess.Popen(
            [
                ps, "-NoProfile", "-Command",
                f"Start-Process explorer.exe 'shell:AppsFolder\\{app_id}'",
            ],
            shell=False,
        )
        return True
    except OSError as e:  # noqa: BLE001
        logger.debug("Start-Process UWP launch failed for %r: %s", app_id, e)
        return False


def _lookup_start_menu(app_name: str) -> str | None:
    """Find an installed app's AppID by its Start Menu display name.

    Runs `Get-StartApps` (PowerShell), which returns every installed app
    (classic, UWP/Store, registered Win32) keyed by friendly Name. We
    match case-insensitively against the query in three phases — exact,
    prefix, substring — and return the first AppID we find.

    Returns None if PowerShell isn't available, the command fails, the
    output can't be parsed, or no name matches.
    """
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return None
    try:
        result = subprocess.run(
            [
                ps, "-NoProfile", "-NonInteractive", "-Command",
                "Get-StartApps | ConvertTo-Json -Compress",
            ],
            capture_output=True, text=True, timeout=10, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug("Get-StartApps failed: %s", e)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        items = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.debug("Get-StartApps JSON parse failed: %s", e)
        return None
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return None

    needle = app_name.strip().lower()

    # Tier 1: exact match.
    for item in items:
        name = (item.get("Name") or "").lower()
        if name == needle:
            return item.get("AppID")

    # Tier 2: name is prefix/substring of needle OR vice versa (bidirectional).
    for item in items:
        name = (item.get("Name") or "").lower()
        if not name:
            continue
        if name.startswith(needle) or needle.startswith(name):
            return item.get("AppID")
        if needle in name or name in needle:
            return item.get("AppID")

    # Tier 3: camelCase / space token-level match. Any token of needle
    # equal to, starting with, or contained in the listed name.
    tokens = [t for t in _split_tokens(needle) if t]
    for item in items:
        name = (item.get("Name") or "").lower()
        if not name:
            continue
        for tok in tokens:
            if tok == name or tok in name or name in tok:
                return item.get("AppID")

    return None


def _split_tokens(s: str) -> list[str]:
    """Split 'SberJazz' -> ['sber','jazz']; 'my-app_v2' -> ['my','app','v2']."""
    import re

    # Split on camelCase boundary AND on any non-alphanumeric separator.
    parts = re.split(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", s)
    return [p.lower() for p in parts if p]


_DEFAULT = _DefaultBackend()


def switch(*, window_title: str, _backend: WindowBackend = _DEFAULT) -> dict[str, Any]:
    ok = _backend.switch(window_title)
    if not ok:
        raise ComputerMCPError(
            ErrorCategory.APP_NOT_FOUND,
            f"window matching '{window_title}' not found",
        )
    return {"state_changed_hint": True}


def resize(
    *,
    window_title: str,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
    maximize: bool = False,
    minimize: bool = False,
    _backend: WindowBackend = _DEFAULT,
) -> dict[str, Any]:
    if minimize:
        ok = _backend.minimize(window_title)
    elif maximize:
        ok = _backend.maximize(window_title)
    else:
        if None in (x, y, width, height):
            raise ComputerMCPError(
                ErrorCategory.INVALID_INPUT,
                "resize requires x/y/width/height unless minimize=true or maximize=true",
            )
        ok = _backend.resize(window_title, x, y, width, height)
    if not ok:
        raise ComputerMCPError(
            ErrorCategory.APP_NOT_FOUND,
            f"window matching '{window_title}' not found",
        )
    return {"state_changed_hint": True}


def launch_app(*, app_name: str, _backend: WindowBackend = _DEFAULT) -> dict[str, Any]:
    # Snapshot top-level HWNDs BEFORE launching so the probe can diff and
    # pick only windows that newly appeared.
    before = _backend.snapshot_hwnds()
    ok = _backend.launch(app_name)
    if not ok:
        raise ComputerMCPError(
            ErrorCategory.APP_NOT_FOUND,
            f"application '{app_name}' not found or failed to start",
        )
    title, hwnd = _backend.probe_launched_window(before=before)
    return {
        "launched": app_name,
        "window_title": title,
        "window_hwnd": hwnd,
        "state_changed_hint": True,
    }
