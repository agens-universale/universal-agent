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

Поэтому здесь потока нет вовсе: на каждом шаге опроса читаем то, что уже
готово, через общий с подтверждениями гарда читатель ядра
(`gc_core.utils.stdin_lines`). Читатель у них один НАРОЧНО: с двумя буферами
строка, пришедшая одному, пропала бы для другого.

Windows и stdin без ``fileno()`` (подменённый в тестах, pipe из чужого кода)
остаются на прежнем потоке-читателе: ``select`` там неприменим к консоли. И
Ctrl+C на этом пути НЕ перехватывается: поток-читатель прервать нечем, и после
перехвата он забрал бы строку у следующего вопроса. Без перехвата Ctrl+C
снимает прогон — честнее, чем украденный ответ.
"""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
from typing import Any, Callable

from gc_core.utils.sigint_waits import intercept_sigint
from gc_core.utils.stdin_lines import discard_unread, pollable_stdin_fd, take_answer

#: Шаг опроса. Человек печатает секундами, 50 мс незаметны, а loop свободен.
#: Опрос сохранён СОЗНАТЕЛЬНО, а не по инерции: на Windows Proactor-loop может
#: не проснуться по Ctrl+C, и именно периодический тик даёт питоновскому
#: обработчику сигнала отработать вовремя.
_POLL_SEC = 0.05


def _read_stdin_lines_into(box: queue.Queue[str]) -> None:
    """Запасной путь (Windows, stdin без fileno): построчно до пустой строки."""
    lines: list[str] = []
    try:
        for raw in sys.stdin:
            line = raw.rstrip("\n")
            if not line.strip():
                break
            lines.append(line)
    except Exception:
        pass
    box.put("\n".join(lines).strip())


async def _ask_via_thread() -> str:
    """Запасной путь: поток-читатель, без перехвата Ctrl+C (см. докстроку модуля)."""
    box: queue.Queue[str] = queue.Queue()
    threading.Thread(
        target=_read_stdin_lines_into,
        args=(box,),
        name="gc-cli-ask-human",
        daemon=True,
    ).start()
    while True:
        try:
            return box.get_nowait()
        except queue.Empty:
            await asyncio.sleep(_POLL_SEC)


def make_cli_ask_human_port() -> Callable[[str], Any]:
    """Корутинный порт для ``ctx.ask_human``."""

    async def _ask(question: str) -> str:  # noqa: ARG001  # часть контракта: подпись менять нельзя
        fd = pollable_stdin_fd()
        if fd is None:
            return await _ask_via_thread()

        interrupted = False

        def _on_sigint() -> None:
            nonlocal interrupted
            interrupted = True

        # Ctrl+C на вопросе — отказ отвечать, а не отмена прогона.
        with intercept_sigint(_on_sigint):
            try:
                while True:
                    if interrupted:
                        raise KeyboardInterrupt
                    answer = take_answer(fd)
                    if answer is not None:
                        return answer
                    await asyncio.sleep(_POLL_SEC)
            except BaseException:
                # Вопрос снят — Ctrl+C или отмена прогона. Набранное к нему уже
                # лежит в общем буфере, и следующим читателем может оказаться
                # подтверждение гарда: оно ответило бы этой строкой за человека.
                discard_unread(fd)
                raise

    return _ask


__all__ = ["make_cli_ask_human_port"]
