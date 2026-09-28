"""Ответы человека из stdin — один читатель на вопросы агента и подтверждения.

Вопрос агента в gc-cli и подтверждение гарда `y/n` читают один и тот же stdin.
Пока у каждого был свой читатель — вопрос через `select`, подтверждение через
`input()` в рабочем потоке, — брошенное по Ctrl+C подтверждение оставалось в
`read()` и забирало строку, набранную уже для следующего вопроса. Прервать
поток, сидящий в блокирующем чтении, нечем. Поэтому читатель здесь один: без
потока, через `select`, с одним буфером на процесс под замком — его трогают и
корутина вопроса на loop, и поток подтверждения.

Читаем ТОЛЬКО когда дескриптор уже готов: терминал в каноническом режиме отдаёт
данные построчно, а вне вопроса stdin не читает никто. Строки, пришедшие одним
куском (труба, вставка из буфера), разбирают последовательные вопросы, а не
один. Но набранное к СНЯТОМУ вопросу следующему не достаётся: оно было ответом
на тот вопрос, и подхвати его подтверждение гарда — вышло бы разрешение без
человека. Поэтому остаток выбрасывается при отмене вопроса и перед `y/n`
(`discard_unread`).

Конец ввода РАЗОВЫЙ. У терминала Ctrl+D заканчивает один ответ — так и сказано в
подсказке тула, — и печатать можно дальше; признак, взведённый навсегда,
превращал весь дальнейший диалог в мгновенные пустые ответы. Недописанная
строка на конце ввода — тоже ответ: `printf 'abc'` даёт «abc», а не пустоту.

Windows и stdin без `fileno()` сюда не попадают: `select` к консоли неприменим,
там вызывающий читает построчно сам.
"""

from __future__ import annotations

import codecs
import os
import sys
import threading
from typing import Callable

_READ_CHUNK = 4096

#: Разобранные, но ещё не отданные строки — общие на процесс. Вопрос забирает
#: из них, сколько нужно ему, остальное достаётся следующему.
_PENDING: list[str] = []
#: Недочитанный хвост строки между тиками.
_PARTIAL = ""
_EOF = False
#: Декодер ОДИН на процесс, а не на вызов: многобайтный символ приходит
#: разорванным между чанками, и свежий декодер молча заменил бы его половинки на
#: U+FFFD. Инкрементальный хранит состояние ровно ради этого.
_DECODER = codecs.lookup("utf-8").incrementaldecoder(errors="replace")
#: Буфер читают из двух мест сразу: корутина вопроса на loop и поток
#: подтверждения гарда. Держится на время неблокирующего чтения — микросекунды.
_LOCK = threading.Lock()


def pollable_stdin_fd() -> int | None:
    """Дескриптор stdin, если у него он есть и `select` к нему применим."""
    if os.name == "nt":
        return None
    try:
        fd = sys.stdin.fileno()
    except Exception:  # подменённый stdin без fileno
        return None
    return fd if isinstance(fd, int) and fd >= 0 else None


def _drain_ready(fd: int) -> None:
    """Прочитать то, что уже готово, и разложить по строкам. Не блокирует."""
    global _PARTIAL, _EOF
    import select

    while True:
        try:
            if not select.select([fd], [], [], 0)[0]:
                return
            chunk = os.read(fd, _READ_CHUNK)
        except (OSError, ValueError):  # stdin закрыли посреди вопроса
            _EOF = True
            return
        if not chunk:
            _EOF = True
            return
        text = _PARTIAL + _DECODER.decode(chunk)
        *ready, _PARTIAL = text.split("\n")
        _PENDING.extend(ready)


def _close_input_end() -> list[str]:
    """Конец ввода закрывает ответ: отдать недописанную строку, снять признак.

    Признак снимается сразу: у терминала конец ввода разовый, и следующий вопрос
    обязан снова ждать человека. У закрытой трубы следующее чтение просто
    увидит конец ввода опять.
    """
    global _PARTIAL, _EOF
    tail = _PARTIAL + _DECODER.decode(b"", final=True)
    _DECODER.reset()
    _PARTIAL = ""
    _EOF = False
    return [tail] if tail else []


def take_answer(fd: int) -> str | None:
    """Ответ на вопрос: строки до пустой строки или до конца ввода.

    ``None`` — ответа ещё нет, надо ждать дальше.
    """
    with _LOCK:
        _drain_ready(fd)
        if "" in _PENDING:
            cut = _PENDING.index("")
            answer = "\n".join(_PENDING[:cut]).strip()
            del _PENDING[: cut + 1]
            return answer
        if _EOF:
            lines = [*_PENDING, *_close_input_end()]
            _PENDING.clear()
            return "\n".join(lines).strip()
        return None


def discard_unread(fd: int) -> None:
    """Выбросить всё непрочитанное: разобранные строки, хвост и готовое в дескрипторе.

    Зовётся в двух местах, и оба — про то, что ввод принадлежит КОНКРЕТНОМУ
    вопросу. Вопрос сняли (Ctrl+C, отмена прогона) — набранное к нему было
    ответом на него и следующему читателю не принадлежит: иначе строка `y`,
    начатая для вопроса агента, отвечала бы за человека на подтверждении гарда
    и записывала разрешение, о котором не спрашивали. Перед вопросом `y/n` —
    подтверждение безопасности принимает только ввод, сделанный ПОСЛЕ того, как
    вопрос показан, а не случайно напечатанное, пока агент работал.
    """
    global _PARTIAL, _EOF
    with _LOCK:
        _drain_ready(fd)
        _PENDING.clear()
        _PARTIAL = ""
        _DECODER.reset()
        _EOF = False


def take_line(fd: int, *, give_up: Callable[[], bool]) -> str | None:
    """Одна строка — ответ на `y/n`. ``None`` — строки ещё нет.

    ``EOFError`` — конец ввода, а строки нет.

    ``give_up`` спрашивается под замком ДО того, как строка будет забрана:
    брошенный читатель не уносит строку, набранную уже для следующего вопроса,
    даже если его бросили ровно между двумя тиками опроса.
    """
    with _LOCK:
        if give_up():
            return None
        _drain_ready(fd)
        if _PENDING:
            return _PENDING.pop(0)
        if _EOF:
            tail = _close_input_end()
            if tail:
                return tail[0]
            raise EOFError
        return None


__all__ = ["discard_unread", "pollable_stdin_fd", "take_answer", "take_line"]
