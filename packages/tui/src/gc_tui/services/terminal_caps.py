"""Что умеет терминал, в котором мы запущены.

Отвечает на два вопроса: какого цвета фон терминала (проба OSC 11) и рисует
ли терминал символы, которых нет в выбранном шрифте. Это не свойство
кодировки — символ доезжает целым в любом случае, — а свойство
терминала: умеет ли он подстановку шрифтов (font fallback), то есть поиск
глифа в других установленных шрифтах системы.

Различать сам ШРИФТ приложение не может: терминал не сообщает, какие глифы в
нём есть, а недостающий символ занимает ровно ту же колонку, что и любой
другой. Поэтому единственный честный признак — какой терминал перед нами.
"""

from __future__ import annotations

import os
import re
import select
import sys
import time
from typing import IO, Any

# Запрос фона (OSC 11) и следом DA1: на DA1 отвечает любой терминал, и ответы
# приходят по порядку, поэтому ответ DA1 значит «фона не будет» — ждать
# таймаута незачем.
_QUERY = b"\x1b]11;?\x1b\\" + b"\x1b[c"
_DA1_REPLY = re.compile(rb"\x1b\[\?[0-9;]*c")
_HEX = rb"([0-9a-fA-F]{1,4})"
# Конец ответа — BEL (Terminal.app) или ST (iTerm2).
_OSC11_REPLY = re.compile(
    rb"\x1b\]11;rgb:"
    + _HEX
    + rb"/"
    + _HEX
    + rb"/"
    + _HEX
    + rb"(?:\x07|\x1b\\)"
)
_READ_CHUNK = 1024


def _os_name() -> str:
    """Имя платформы; отдельная точка подмены для кроссплатформенных тестов."""
    return os.name


def supports_font_fallback() -> bool:
    """True, если терминал сам найдёт глиф, которого нет в основном шрифте.

    Windows-консоль (conhost — окно `powershell.exe`, `cmd.exe`) рисует
    единственным шрифтом из своих свойств: Consolas, Lucida Console, Courier
    New. Символа вне их покрытия она заменит пустым прямоугольником.

    Windows Terminal и встроенный терминал VS Code подстановку делают, поэтому
    им можно отдавать символы из редких блоков Unicode. Терминалы Linux и macOS
    подстановку делают повсеместно.

    Цена ошибки несимметрична: лишний раз ответить False значит показать более
    простой символ, а лишний раз True — показать пустой прямоугольник. Поэтому
    при любых сомнениях отвечаем False.
    """
    if os.environ.get("WT_SESSION"):  # Windows Terminal
        return True
    if (
        os.environ.get("TERM_PROGRAM") == "vscode"
    ):  # встроенный терминал VS Code
        return True
    if _os_name() == "nt":  # conhost: один шрифт, без подстановки
        return False
    # WSL: платформа posix, но рисует всё равно терминал Windows. Если это
    # был бы Windows Terminal, нас поймала бы ветка WT_SESSION выше; раз не
    # поймала — терминал неизвестен, и это вполне может быть conhost.
    return not (
        os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP")
    )


def parse_osc11(data: bytes) -> tuple[int, int, int] | None:
    """Цвет из ответа на OSC 11 (`rgb:RRRR/GGGG/BBBB`), каналы в 0..255.

    Компонент — 1–4 hex-цифры; шкала зависит от их числа (`f` и `ffff` —
    оба 255), от 16-битного канала берётся старший байт. Ответа в данных
    нет — None.
    """
    match = _OSC11_REPLY.search(data)
    if match is None:
        return None
    red, green, blue = (_scale(group) for group in match.groups())
    return red, green, blue


def _scale(digits: bytes) -> int:
    """Компонент в 0..255: дополнить до 16 бит повтором цифр (`f` → `ffff`,
    `80` → `8080`) и взять старший байт."""
    return int((digits * 4)[:4], 16) >> 8


def _tty_fds(stdin: Any, stdout: Any) -> tuple[int, int] | None:
    """Дескрипторы ввода и вывода, если с ними можно говорить как с
    терминалом; иначе None."""
    if sys.platform == "win32":
        return None
    if os.environ.get("TERM", "") in ("", "dumb"):
        return None
    try:
        fds = (
            (sys.stdin if stdin is None else stdin).fileno(),
            (sys.stdout if stdout is None else stdout).fileno(),
        )
    except (AttributeError, OSError, ValueError):
        return None
    return fds if all(os.isatty(fd) for fd in fds) else None


def _read_until_da1(fd: int, timeout: float) -> bytes:
    """Читать ввод до ответа на DA1 или до таймаута."""
    deadline = time.monotonic() + timeout
    data = b""
    while _DA1_REPLY.search(data) is None:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            break
        chunk = os.read(fd, _READ_CHUNK)
        if not chunk:
            break
        data += chunk
    return data


def query_background(
    *,
    timeout: float = 0.1,
    stdin: IO[Any] | None = None,
    stdout: IO[Any] | None = None,
) -> tuple[int, int, int] | None:
    """Цвет фона терминала (каналы 0..255) или None, если узнать нельзя.

    Не TTY, Windows, `TERM` пустой или `dumb` — None без записи в терминал.
    Терминал, который OSC 11 не знает, ответит только на DA1 — None сразу;
    молчащий (tmux/screen без проброса) — None по таймауту. Режим терминала
    восстанавливается всегда, и с ним сбрасывается непрочитанный ввод:
    поздний ответ не должен попасть во ввод приложения как нажатия клавиш.
    """
    fds = _tty_fds(stdin, stdout)
    if fds is None:
        return None
    # Только после проверки платформы: на Windows этих модулей нет.
    import termios
    import tty

    in_fd, out_fd = fds
    try:
        saved = termios.tcgetattr(in_fd)
    except termios.error:
        return None
    try:
        tty.setcbreak(in_fd, termios.TCSANOW)
        os.write(out_fd, _QUERY)
        return parse_osc11(_read_until_da1(in_fd, timeout))
    except (OSError, termios.error):
        return None
    finally:
        try:
            termios.tcsetattr(in_fd, termios.TCSAFLUSH, saved)
        except (OSError, termios.error):
            pass  # терминал умер — восстанавливать нечего, итог пробы цел
