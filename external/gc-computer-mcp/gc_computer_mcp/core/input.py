"""Thin wrappers over pyautogui for mouse/keyboard. Centralised for testability."""

from __future__ import annotations

import logging
import sys
import time

import pyautogui

logger = logging.getLogger(__name__)

# Disable pyautogui's fail-safe: for automated operation we trust the caller.
# If re-enabling becomes desirable, do it via config.
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0  # we time ourselves with timeout_guard


def click(x: int, y: int, button: str = "left", clicks: int = 1) -> None:
    pyautogui.click(x=x, y=y, button=button, clicks=clicks)


def move_to(x: int, y: int) -> None:
    pyautogui.moveTo(x, y)


def type_text(text: str) -> None:
    """Send text as unicode SendInput events. Layout-independent, no clipboard.

    Tried earlier: `pyautogui.typewrite` (broken on non-US layouts) and
    clipboard+Ctrl+V (unreliable with WinUI3 apps like the Windows 11
    Notepad — timing-sensitive, often lands empty). SendInput with
    KEYEVENTF_UNICODE sends the actual unicode code point to the focused
    window via the Win32 API; no scan codes, no clipboard, no paste
    shortcut. Works for classic Win32, UWP, WinUI3, Electron, web views,
    and Office apps.

    Characters outside the BMP (e.g. some emoji) are transparently split
    into UTF-16 surrogate pairs by `ord`-on-each-char logic below —
    Python strings iterate code points, and Windows expects UTF-16 code
    units, so we encode each char to UTF-16-LE and emit one event per
    unit. This handles astral-plane characters correctly.
    """
    if not text:
        return

    if sys.platform != "win32":
        # Fallback for non-Windows (CI, dev boxes): pyautogui.typewrite.
        # Tests run on Windows so this path is rarely exercised; leave
        # behaviour pragmatic rather than erroring.
        pyautogui.typewrite(text)
        return

    _send_unicode(text)


def _send_unicode(text: str) -> None:
    """Win32 SendInput with KEYEVENTF_UNICODE, one event at a time.

    Earlier versions batched all events into a single SendInput call; on
    at least one machine that produced repeated-last-char garbage
    ("Hello, World" -> "Hello,dddddd"). Sending per-character with a tiny
    sleep avoids any queue/batch pathology at the cost of ~1ms/char
    overhead — immaterial for typical text lengths.
    """
    import ctypes
    from ctypes import wintypes

    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP = 0x0002
    INPUT_KEYBOARD = 1

    ULONG_PTR = ctypes.c_void_p  # matches ULONG_PTR on both x86 and x64

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [
            ("type", wintypes.DWORD),
            ("ii", _INPUT_UNION),
        ]

    SendInput = ctypes.windll.user32.SendInput
    SendInput.argtypes = [
        wintypes.UINT,
        ctypes.POINTER(INPUT),
        ctypes.c_int,
    ]
    SendInput.restype = wintypes.UINT

    def _send_one(unit: int, key_up: bool) -> None:
        ip = INPUT()
        ip.type = INPUT_KEYBOARD
        ip.ii.ki.wVk = 0
        ip.ii.ki.wScan = unit
        ip.ii.ki.dwFlags = (
            KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if key_up else 0)
        )
        ip.ii.ki.time = 0
        ip.ii.ki.dwExtraInfo = 0
        sent = SendInput(1, ctypes.byref(ip), ctypes.sizeof(INPUT))
        if sent != 1:
            err = ctypes.get_last_error()
            logger.warning(
                "SendInput failed for unit 0x%04x (sent=%d, GetLastError=%d)",
                unit, sent, err,
            )

    # Encode to UTF-16-LE and iterate 16-bit code units so astral chars
    # (surrogate pairs) are emitted correctly.
    encoded = text.encode("utf-16-le")
    code_units = [
        int.from_bytes(encoded[i:i + 2], "little")
        for i in range(0, len(encoded), 2)
    ]

    for unit in code_units:
        _send_one(unit, key_up=False)
        _send_one(unit, key_up=True)
        time.sleep(0.001)  # 1ms per char — avoids input-queue coalescing

    # Final pause for the target window to process all events before the
    # next MCP tool call observes state.
    time.sleep(0.05)


# Win32 Virtual-Key codes for our hotkey / press_key layer.
# Enough to cover computer_shortcut use cases (Ctrl+C/V/S/Z, Alt+Tab, F-keys)
# plus the common computer_key set (enter, tab, esc, arrows, delete, ...).
_VK_CODES: dict[str, int] = {
    # modifiers
    "ctrl": 0x11, "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B, "windows": 0x5B, "meta": 0x5B, "cmd": 0x5B,
    # whitespace / editing
    "enter": 0x0D, "return": 0x0D,
    "tab": 0x09,
    "escape": 0x1B, "esc": 0x1B,
    "backspace": 0x08, "bksp": 0x08, "bs": 0x08,
    "delete": 0x2E, "del": 0x2E,
    "space": 0x20, "spacebar": 0x20,
    # navigation
    "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "insert": 0x2D, "ins": 0x2D,
    # function keys
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    # letters A..Z -> 0x41..0x5A
    **{chr(ord("a") + i): 0x41 + i for i in range(26)},
    # digits 0..9 -> 0x30..0x39
    **{str(i): 0x30 + i for i in range(10)},
    # punctuation (US layout — common shortcuts)
    "plus": 0xBB, "minus": 0xBD, "equals": 0xBB,
    ",": 0xBC, ".": 0xBE, "/": 0xBF, ";": 0xBA,
}


def _resolve_vk(key: str) -> int:
    vk = _VK_CODES.get(str(key).strip().lower())
    if vk is None:
        raise ValueError(
            f"unknown key {key!r}; supported: {sorted(_VK_CODES)}"
        )
    return vk


def _send_vk_sequence(events: list[tuple[int, bool]]) -> None:
    """Send a sequence of (vk_code, key_up) events via Win32 SendInput.

    One SendInput call per event with a short inter-event sleep so the
    target window has time to register each transition. Fixes Ctrl+V
    miss in Telegram/Qt and similar Electron apps where pyautogui's
    zero-pause keyDown/keyUp lands faster than the target's focus sync.
    """
    import ctypes
    from ctypes import wintypes

    KEYEVENTF_KEYUP = 0x0002
    INPUT_KEYBOARD = 1
    ULONG_PTR = ctypes.c_void_p

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("ii", _INPUT_UNION)]

    SendInput = ctypes.windll.user32.SendInput
    SendInput.argtypes = [
        wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int,
    ]
    SendInput.restype = wintypes.UINT

    for vk, key_up in events:
        ip = INPUT()
        ip.type = INPUT_KEYBOARD
        ip.ii.ki.wVk = vk
        ip.ii.ki.wScan = 0
        ip.ii.ki.dwFlags = KEYEVENTF_KEYUP if key_up else 0
        ip.ii.ki.time = 0
        ip.ii.ki.dwExtraInfo = 0
        sent = SendInput(1, ctypes.byref(ip), ctypes.sizeof(INPUT))
        if sent != 1:
            err = ctypes.get_last_error()
            logger.warning(
                "SendInput failed for vk=0x%02x up=%s (sent=%d, err=%d)",
                vk, key_up, sent, err,
            )
        time.sleep(0.01)
    # Final pause so the target window finishes processing the hotkey
    # before the next MCP tool call observes state.
    time.sleep(0.05)


def press_key(key: str) -> None:
    if sys.platform != "win32":
        pyautogui.press(key)
        return
    vk = _resolve_vk(key)
    _send_vk_sequence([(vk, False), (vk, True)])


def hotkey(keys: list[str]) -> None:
    if sys.platform != "win32":
        pyautogui.hotkey(*keys)
        return
    vks = [_resolve_vk(k) for k in keys]
    # keyDown in order, then keyUp in reverse — releases modifiers last.
    events: list[tuple[int, bool]] = [(vk, False) for vk in vks]
    events += [(vk, True) for vk in reversed(vks)]
    _send_vk_sequence(events)


def scroll(direction: str, amount: int = 3) -> None:
    """Vertical: up/down via pyautogui.scroll. Horizontal: left/right via pyautogui.hscroll.

    Amount is in wheel notches. Positive values scroll up/right; negative down/left.
    """
    if direction == "up":
        pyautogui.scroll(amount)
    elif direction == "down":
        pyautogui.scroll(-amount)
    elif direction == "right":
        pyautogui.hscroll(amount)
    elif direction == "left":
        pyautogui.hscroll(-amount)
    else:
        raise ValueError(f"unknown scroll direction: {direction!r}")


def drag(from_xy: tuple[int, int], to_xy: tuple[int, int], button: str = "left") -> None:
    pyautogui.moveTo(*from_xy)
    pyautogui.dragTo(*to_xy, button=button)
