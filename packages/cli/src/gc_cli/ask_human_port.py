"""Порт «спросить человека» для gc-cli: свой поток для stdin и Ctrl+C как отказ.

Без порта ядро читает stdin само — через ``asyncio.to_thread``. В CLI это ломается
дважды, и оба раза видно пользователю:

* поток берётся из пула ``concurrent.futures``, а его ``asyncio.run`` дожидается
  на выходе. Прерванный прогон не заканчивался, пока в stdin не придёт строка;
* SIGINT под ``asyncio.run`` отменяет главную задачу, а не поднимает
  ``KeyboardInterrupt`` внутри тула. Ветка «прерывание» в ``turn_ask_human``
  оставалась мёртвой: Ctrl+C на вопросе ронял весь прогон, вместо того чтобы
  вернуть агенту «отвечать не стали».

Здесь: чтение в daemon-потоке (интерпретатор его не ждёт), ожидание опросом (loop
свободен), а на время вопроса — свой обработчик SIGINT, который превращает Ctrl+C
в ``KeyboardInterrupt`` ВНУТРИ корутины порта. Дальше его ловит `acall` тула и
отдаёт агенту штатный «interrupted».
"""
from __future__ import annotations

import asyncio
import queue
import signal
import sys
import threading
from typing import Any, Callable

#: Шаг опроса очереди. Человек печатает секундами, 50 мс незаметны, а loop
#: остаётся свободным для остальных задач приложения.
_POLL_SEC = 0.05


def _read_stdin_lines_into(box: "queue.Queue[str]") -> None:
    """Построчное чтение до пустой строки или EOF — контракт ядра для stdin."""
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


def make_cli_ask_human_port() -> Callable[[str], Any]:
    """Корутинный порт для ``ctx.ask_human``."""

    async def _ask(question: str) -> str:
        box: "queue.Queue[str]" = queue.Queue()
        threading.Thread(
            target=_read_stdin_lines_into,
            args=(box,),
            name="gc-cli-ask-human",
            daemon=True,
        ).start()

        interrupted = threading.Event()
        loop = asyncio.get_running_loop()
        previous = signal.getsignal(signal.SIGINT)
        armed = False
        try:
            loop.add_signal_handler(signal.SIGINT, interrupted.set)
            armed = True
        except (NotImplementedError, RuntimeError):
            # Windows или чужой loop: остаёмся без перехвата — хуже, чем было,
            # не станет, ядро просто увидит ответ или EOF.
            pass

        try:
            while True:
                if interrupted.is_set():
                    raise KeyboardInterrupt
                try:
                    return box.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(_POLL_SEC)
        finally:
            if armed:
                try:
                    loop.remove_signal_handler(signal.SIGINT)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    signal.signal(signal.SIGINT, previous)
                except Exception:  # noqa: BLE001 — не главный поток
                    pass

    return _ask


__all__ = ["make_cli_ask_human_port"]
