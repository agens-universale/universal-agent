"""Кроссплатформенный системный буфер обмена (текст, изображение) и открытие файла
во внешнем просмотрщике ОС."""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
from dataclasses import dataclass

_log = logging.getLogger(__name__)


def _run_capture(cmd: list[str]) -> str:
    """Запустить команду, вернуть stdout (utf-8) или '' при любой ошибке."""
    try:
        out = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            timeout=2.0,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.decode("utf-8", errors="replace")


def _run_feed(cmd: list[str], text: str) -> bool:
    """Передать text в stdin команды (utf-8). True при успехе, False при ошибке."""
    try:
        result = subprocess.run(
            cmd,
            input=text.encode("utf-8"),
            check=False,
            capture_output=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def _win_read_clipboard() -> str:
    """Прочитать буфер обмена Windows через Win32 API (CF_UNICODETEXT).

    Без subprocess: запуск PowerShell на Windows сбрасывал кодовую страницу и
    ENABLE_MOUSE_INPUT консоли (спец-глифы TUI → '?', мышь отваливалась)."""
    try:
        import ctypes
        from ctypes import wintypes

        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        u32.GetClipboardData.restype = wintypes.HANDLE
        u32.GetClipboardData.argtypes = [wintypes.UINT]
        k32.GlobalLock.restype = wintypes.LPVOID
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        if not u32.OpenClipboard(None):
            return ""
        try:
            handle = u32.GetClipboardData(_CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = k32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                k32.GlobalUnlock(handle)
        finally:
            u32.CloseClipboard()
    except Exception:
        return ""


def _win_write_clipboard(text: str) -> bool:
    """Записать text в буфер обмена Windows через Win32 API (CF_UNICODETEXT).
    Без subprocess — см. _win_read_clipboard."""
    try:
        import ctypes
        from ctypes import wintypes

        u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
        u32.SetClipboardData.restype = wintypes.HANDLE
        u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        k32.GlobalAlloc.restype = wintypes.HGLOBAL
        k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k32.GlobalLock.restype = wintypes.LPVOID
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalFree.argtypes = [wintypes.HGLOBAL]

        data = text.encode("utf-16-le") + b"\x00\x00"
        h = k32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        if not h:
            return False
        ptr = k32.GlobalLock(h)
        if not ptr:
            k32.GlobalFree(h)
            return False
        ctypes.memmove(ptr, data, len(data))
        k32.GlobalUnlock(h)
        if not u32.OpenClipboard(None):
            k32.GlobalFree(h)
            return False
        try:
            u32.EmptyClipboard()
            if not u32.SetClipboardData(_CF_UNICODETEXT, h):
                k32.GlobalFree(h)
                return False
            return (
                True  # на успехе владение h переходит буферу — не освобождаем
            )
        finally:
            u32.CloseClipboard()
    except Exception:
        return False


def write_clipboard(text: str) -> bool:
    """Записать text в системный буфер обмена нативным средством ОС. True при успехе.

    Дополняет OSC 52 (`App.copy_to_clipboard`): нативный путь кладёт текст в буфер
    локальной машины независимо от поддержки OSC 52 терминалом (Terminal.app без
    OSC 52, корп-Windows и т.п.). OSC 52 остаётся для SSH/удалённых сессий.
    """
    if sys.platform == "win32":
        return _win_write_clipboard(text)
    if sys.platform == "darwin":
        return _run_feed(["pbcopy"], text)
    if sys.platform.startswith("linux"):
        # xclip без -loops 1 может держать процесс; timeout → ложный False.
        for cmd in (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard", "-i", "-loops", "1"],
            ["xsel", "-b", "-i"],
        ):
            if _run_feed(cmd, text):
                return True
    return False


def read_clipboard() -> str:
    """Прочитать системный буфер обмена. Кроссплатформенно; '' если адаптер недоступен.

    Нужен как fallback для Ctrl+V в терминалах, доставляющих ctrl+v как key-event
    (а не bracketed-paste): встроенный TextArea.action_paste читает только
    внутренний app.clipboard, пустой для текста, скопированного вне приложения.
    """
    if sys.platform == "win32":
        return _win_read_clipboard().rstrip("\r\n")
    if sys.platform == "darwin":
        return _run_capture(["pbpaste"])
    if sys.platform.startswith("linux"):
        # --type text: в буфере только картинка — wl-paste без типа отдал бы её байты.
        for cmd in (
            ["wl-paste", "--no-newline", "--type", "text"],
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "-b"],
        ):
            text = _run_capture(cmd)
            if text:
                return text
    return ""


#: Потолок ожидания внешней утилиты буфера (osascript, wl-paste, xclip): зависший
#: клипборд-менеджер не должен держать вставку дольше.
_IMAGE_READ_TIMEOUT = 5.0

# macOS, один процесс на всё: сначала скопированные в Finder файлы — пути, по одному в
# строке (в буфере рядом лежит и значок файла в TIFF — его брать нельзя); затем
# картинка — PNG (скриншоты, браузеры), нет его — TIFF (часть приложений кладёт только
# TIFF). Данные osascript печатает как «data PNGf<hex>» — декодируем hex.
_MAC_CLIPBOARD_IMAGE_SCRIPT = (
    'use framework "AppKit"\n'
    "use scripting additions\n"
    "set pb to current application's NSPasteboard's generalPasteboard()\n"
    "set urls to pb's readObjectsForClasses:{current application's NSURL} "
    "options:{NSPasteboardURLReadingFileURLsOnlyKey:true}\n"
    "if urls is not missing value and (urls's |count|()) > 0 then\n"
    "    set AppleScript's text item delimiters to linefeed\n"
    '    return ((urls\'s valueForKey:"path") as list) as text\n'
    "end if\n"
    "try\n"
    "    return the clipboard as «class PNGf»\n"
    "end try\n"
    "return the clipboard as «class TIFF»"
)
_MAC_DATA_PREFIX = "«data ".encode()
_MAC_DATA_SUFFIX = "»".encode()
_MAC_TYPE_CODE_LEN = 4  # PNGf / TIFF

# Linux: Wayland (и WSLg) — wl-paste, X11 — xclip.
_LINUX_IMAGE_COMMANDS = (
    ["wl-paste", "-t", "image/png"],
    ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
)


class ClipboardToolMissing(RuntimeError):
    """Linux/WSLg: нет ни wl-paste, ни xclip — изображение из буфера не прочитать."""


def _run_binary(cmd: list[str]) -> bytes | None:
    """stdout утилиты байтами; None — ненулевой код, таймаут или сбой запуска.

    Не _run_capture: вывод бинарный, таймаут 5 с, stdin закрыт — дочерний процесс не
    делит с TUI терминал (сырой режим ввода, мышь). Утилиты нет — FileNotFoundError
    наружу: Linux отличает «нечем читать» от «нет картинки»."""
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_IMAGE_READ_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        raise
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _win_read_clipboard_image() -> bytes | list[str] | None:
    """Windows: Pillow ImageGrab.grabclipboard — Win32 API из C, без subprocess (см.
    _win_read_clipboard: PowerShell сбивал кодовую страницу и мышь консоли). Картинка →
    PNG-байты; скопированные в Проводнике файлы (CF_HDROP) → список путей."""
    from PIL import (  # Pillow — зависимость gc-core; импорт ленивый
        Image,
        ImageGrab,
    )

    try:
        grabbed = ImageGrab.grabclipboard()
        if isinstance(grabbed, list):
            return grabbed or None
        if not isinstance(grabbed, Image.Image):
            return None
        buf = io.BytesIO()
        # Промежуточный PNG: нормализация всё равно перекодирует — сжатие минимальное.
        grabbed.save(buf, format="PNG", compress_level=1)
    except Exception:
        _log.warning("clipboard image: cannot read", exc_info=True)
        return None
    return buf.getvalue()


def _mac_read_clipboard_image() -> bytes | list[str] | None:
    try:
        out = _run_binary(["osascript", "-e", _MAC_CLIPBOARD_IMAGE_SCRIPT])
    except FileNotFoundError:
        return None
    if not out:
        return None
    if not out.startswith(_MAC_DATA_PREFIX):
        paths = out.decode("utf-8", errors="replace").splitlines()
        return (
            paths if paths and all(os.path.isabs(p) for p in paths) else None
        )
    out = out.strip()
    if not out.endswith(_MAC_DATA_SUFFIX):
        return None
    digits = out[
        len(_MAC_DATA_PREFIX) + _MAC_TYPE_CODE_LEN : -len(_MAC_DATA_SUFFIX)
    ]
    try:
        data = bytes.fromhex(digits.decode("ascii"))
    except ValueError:
        return None
    return data or None


def _linux_read_clipboard_image() -> bytes | None:
    missing = 0
    for cmd in _LINUX_IMAGE_COMMANDS:
        try:
            data = _run_binary(cmd)
        except FileNotFoundError:
            missing += 1
            continue
        if data:
            return data
    if missing == len(_LINUX_IMAGE_COMMANDS):
        raise ClipboardToolMissing("нет ни wl-paste, ни xclip")
    return None


def read_clipboard_image() -> bytes | list[str] | None:
    """Изображение из системного буфера обмена: bytes — картинка как есть (нормализует
    вызывающий), list — пути скопированных файлов (Windows — CF_HDROP, macOS — файлы
    из Finder), None — картинки нет.

    Блокирует до 5 с (внешняя утилита) — звать вне UI-потока (`asyncio.to_thread`).
    Linux/WSLg без wl-paste и xclip — ClipboardToolMissing."""
    if sys.platform == "win32":
        return _win_read_clipboard_image()
    if sys.platform == "darwin":
        return _mac_read_clipboard_image()
    if sys.platform.startswith("linux"):
        return _linux_read_clipboard_image()
    return None


@dataclass(frozen=True)
class ClipboardProbe:
    """Что лежит в буфере обмена — без чтения самой картинки. `stamp` —
    метка состояния буфера: новое копирование даёт новую метку. `image` — картинка
    данными, `text` — в буфере есть и текст (его возьмёт Ctrl+V), `files` — пути
    скопированных файлов (Finder, Проводник)."""

    stamp: str
    image: bool = False
    text: bool = False
    files: tuple[str, ...] = ()


# macOS: метка — changeCount буфера; скопированные файлы — их пути, иначе типы данных.
# Вывод: метка, строка признаков («files» либо «image»/«text» через пробел), пути.
_MAC_CLIPBOARD_PROBE_SCRIPT = (
    'use framework "AppKit"\n'
    "use scripting additions\n"
    "set pb to current application's NSPasteboard's generalPasteboard()\n"
    "set out to {(pb's changeCount()) as text}\n"
    "set urls to pb's readObjectsForClasses:{current application's NSURL} "
    "options:{NSPasteboardURLReadingFileURLsOnlyKey:true}\n"
    "if urls is not missing value and (urls's |count|()) > 0 then\n"
    '    set end of out to "files"\n'
    '    set out to out & ((urls\'s valueForKey:"path") as list)\n'
    "else\n"
    "    set types to (pb's types()) as list\n"
    "    set kinds to {}\n"
    '    if types contains "public.png" or types contains "public.tiff" then '
    'set end of kinds to "image"\n'
    '    if types contains "public.utf8-plain-text" then set end of kinds to "text"\n'
    '    set AppleScript\'s text item delimiters to " "\n'
    "    set end of out to kinds as text\n"
    "end if\n"
    "set AppleScript's text item delimiters to linefeed\n"
    "return out as text"
)


def _mac_probe_clipboard_image() -> ClipboardProbe | None:
    try:
        out = _run_binary(["osascript", "-e", _MAC_CLIPBOARD_PROBE_SCRIPT])
    except FileNotFoundError:
        return None
    lines = (out or b"").decode("utf-8", errors="replace").splitlines()
    if len(lines) < 2 or not lines[0].isdigit():
        return None
    stamp, flags = lines[0], lines[1].split()
    if "files" in flags:
        return ClipboardProbe(stamp, files=tuple(lines[2:]))
    return ClipboardProbe(stamp, image="image" in flags, text="text" in flags)


def _win_clipboard_sequence() -> int:
    """Номер состояния буфера Windows; 0 — нет доступа к буферу."""
    import ctypes

    return ctypes.windll.user32.GetClipboardSequenceNumber()


def _win_probe_clipboard_image() -> ClipboardProbe | None:
    """Windows: метка — номер состояния буфера (дёшево, без открытия буфера); что в нём —
    Pillow, как у вставки (`_win_read_clipboard_image`)."""
    from PIL import (  # Pillow — зависимость gc-core; импорт ленивый
        Image,
        ImageGrab,
    )

    try:
        stamp = _win_clipboard_sequence()
        if not stamp:
            return None
        grabbed = ImageGrab.grabclipboard()
    except Exception:
        _log.debug("clipboard probe: cannot read", exc_info=True)
        return None
    if isinstance(grabbed, list):
        return ClipboardProbe(str(stamp), files=tuple(grabbed))
    return ClipboardProbe(str(stamp), image=isinstance(grabbed, Image.Image))


def probe_clipboard_image() -> ClipboardProbe | None:
    """Что лежит в буфере обмена, для подсказки о вставке; None — не узнать (нет доступа,
    утилиты, сбой). Блокирует (внешняя утилита, до 5 с) — звать вне UI-потока.

    Linux — None без утилит: буфер там читается только вставкой. Метки буфера нет —
    проба читала бы картинку целиком, а wl-paste в GNOME на Wayland на миг отбирает
    фокус у терминала, и возвращение фокуса запускало бы пробу снова."""
    if sys.platform == "win32":
        return _win_probe_clipboard_image()
    if sys.platform == "darwin":
        return _mac_probe_clipboard_image()
    return None


def copied_files_come_with_names() -> bool:
    """Скопированные файлы кладут рядом свои имена текстом — macOS (Finder). Тогда
    Ctrl+V при тексте в буфере сначала ищет файлы. Проводник Windows текста к файлам
    не кладёт, на Linux скопированные файлы вставка не берёт."""
    return sys.platform == "darwin"


def read_clipboard_files() -> tuple[str, ...]:
    """Пути скопированных файлов (Finder, Проводник); пусто — их нет или не узнать
    (Linux — всегда пусто, см. `probe_clipboard_image`). Блокирует (внешняя
    утилита, до 5 с) — звать вне UI-потока."""
    probe = probe_clipboard_image()
    return probe.files if probe is not None else ()


def open_path(path: str) -> bool:
    """Открыть файл во внешнем просмотрщике ОС (Ctrl+клик по image-плашке). False — файла
    нет или запуск не удался. Просмотрщик не ждём; дочерний процесс — без терминала TUI
    (stdin/stdout/stderr закрыты, своя сессия), на Windows — ShellExecute без консоли."""
    if not os.path.isfile(path):
        return False
    if sys.platform == "win32":
        try:
            os.startfile(path)
        except OSError:
            _log.warning(
                "open_path: os.startfile failed for %s", path, exc_info=True
            )
            return False
        return True
    cmd = ["open", path] if sys.platform == "darwin" else ["xdg-open", path]
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        _log.warning(
            "open_path: %s failed for %s", cmd[0], path, exc_info=True
        )
        return False
    return True
