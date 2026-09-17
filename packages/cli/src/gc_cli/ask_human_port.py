"""Порт «спросить человека» для gc-cli: stdin читается только во время вопроса.

Ядро без порта читает stdin само, через ``asyncio.to_thread``, и в CLI это
ломается дважды: поток берётся из общего пула, который ``asyncio.run`` ждёт на
выходе, а SIGINT отменяет главную задачу вместо ``KeyboardInterrupt`` внутри
тула — Ctrl+C ронял весь прогон.

Первая редакция порта заводила ДЕМОН-ПОТОК на каждый вопрос. Она чинила выход и
Ctrl+C, но принесла свой дефект: отменить поток, сидящий в ``readline``, нечем.
После Ctrl+C он оставался в ``read()`` и забирал первую строку у следующего
читателя — у следующего вопроса или у подтверждения ``y/n`` гарда. Человек
отвечал, а ответ уходил в никуда.

Поэтому здесь потока нет вовсе. На каждом шаге опроса спрашиваем ``select``,
готов ли дескриптор, и читаем ТОЛЬКО когда строка уже есть: терминал в
каноническом режиме отдаёт данные построчно. Инвариант возвращается к тому, что
было на develop — **вне вопроса stdin не читает никто**, поэтому набранное к
прерванному вопросу остаётся в трубе для следующего читателя, а не пропадает.

Строки, пришедшие одним куском (труба, файл, вставка из буфера), хранятся в
общем на процесс буфере: их разберут последовательные вопросы, а не один.

Windows и stdin без ``fileno()`` (подменённый в тестах, pipe из чужого кода)
остаются на прежнем потоке-читателе: ``select`` там неприменим к консоли.
"""
from __future__ import annotations

import asyncio
import codecs
import os
import queue
import signal
import sys
import threading
from typing import Any, Callable

#: Шаг опроса. Человек печатает секундами, 50 мс незаметны, а loop свободен.
#: Опрос сохранён СОЗНАТЕЛЬНО, а не по инерции: на Windows Proactor-loop может
#: не проснуться по Ctrl+C, и именно периодический тик даёт питоновскому
#: обработчику сигнала отработать вовремя.
_POLL_SEC = 0.05

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


def _stdin_fd() -> int | None:
    """Дескриптор stdin, если у него он есть и `select` к нему применим."""
    if os.name == "nt":
        return None
    try:
        fd = sys.stdin.fileno()
    except Exception:  # noqa: BLE001 — подменённый stdin без fileno
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
        except (OSError, ValueError):       # stdin закрыли посреди вопроса
            _EOF = True
            return
        if not chunk:
            _EOF = True
            return
        text = _PARTIAL + _DECODER.decode(chunk)
        *ready, _PARTIAL = text.split("\n")
        _PENDING.extend(ready)


def _take_answer() -> str | None:
    """Собрать ответ из разобранных строк: до пустой строки или EOF.

    ``None`` — ответа ещё нет, надо ждать дальше.
    """
    if "" in _PENDING:
        cut = _PENDING.index("")
        answer = "\n".join(_PENDING[:cut]).strip()
        del _PENDING[: cut + 1]
        return answer
    if _EOF:
        answer = "\n".join(_PENDING).strip()
        _PENDING.clear()
        return answer
    return None


def _read_stdin_lines_into(box: "queue.Queue[str]") -> None:
    """Запасной путь (Windows, stdin без fileno): построчно до пустой строки."""
    lines: list[str] = []
    try:
        for raw in sys.stdin:
            line = raw.rstrip("\n")
            if not line.strip():
                break
            lines.append(line)
    except Exception:  # noqa: BLE001 — stdin закрыли; отдаём, что успели
        pass
    box.put("\n".join(lines).strip())


class _Interrupt:
    """Перехват SIGINT на время вопроса: Ctrl+C — отказ отвечать, не отмена.

    ``signal.signal``, а не ``loop.add_signal_handler``: первым пользуется сам
    ``asyncio.Runner``, и он работает там, где у loop поддержки сигналов нет —
    то есть на Windows. Ставится только из главного потока, иначе ValueError.
    """

    def __init__(self) -> None:
        self.hit = False
        self._previous: Any = None
        self._armed = False

    def __enter__(self) -> "_Interrupt":
        if threading.current_thread() is not threading.main_thread():
            return self
        try:
            self._previous = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._on_sigint)
            self._armed = True
        except (ValueError, OSError, AttributeError):
            self._armed = False
        return self

    def _on_sigint(self, _signum: int, _frame: Any) -> None:
        self.hit = True

    def __exit__(self, *_exc: Any) -> None:
        if not self._armed:
            return
        try:
            signal.signal(signal.SIGINT, self._previous)
        except (ValueError, OSError):
            pass


def make_cli_ask_human_port() -> Callable[[str], Any]:
    """Корутинный порт для ``ctx.ask_human``."""

    async def _ask(question: str) -> str:
        fd = _stdin_fd()
        box: "queue.Queue[str]" | None = None
        if fd is None:
            box = queue.Queue()
            threading.Thread(
                target=_read_stdin_lines_into, args=(box,),
                name="gc-cli-ask-human", daemon=True,
            ).start()

        with _Interrupt() as interrupt:
            while True:
                if interrupt.hit:
                    raise KeyboardInterrupt
                if box is not None:
                    try:
                        return box.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(_POLL_SEC)
                        continue
                _drain_ready(fd)
                answer = _take_answer()
                if answer is not None:
                    return answer
                await asyncio.sleep(_POLL_SEC)

    return _ask


__all__ = ["make_cli_ask_human_port"]
