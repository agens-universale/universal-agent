"""Перехват Ctrl+C на время ожидания человека — один обработчик на процесс.

Пока ждут человека — подтверждения гарда, ответа на вопрос агента в gc-cli, —
Ctrl+C значит «не буду отвечать», а не «останови прогон». Раньше каждое
ожидание само запоминало обработчик на входе и возвращало его на выходе. Два
ожидания на одном loop (несколько агентов в SDK, сервер) выходят не в порядке
входа и затирали друг другу обработчик:

  * вышедшее первым возвращало обработчик, стоявший ДО обоих, — и пока второе
    ещё ждало человека, Ctrl+C снимал прогон;
  * вышедшее вторым возвращало обработчик первого, уже мёртвый: он писал в
    отменённый future и молча глотал всё, так что Ctrl+C до конца процесса не
    делал ничего.

Поэтому обработчик один: его ставит первое ожидание и снимает последнее, а
сигнал достаётся самому позднему из ещё открытых. Порядок выхода значения не
имеет — ожидание снимается из стека по идентичности.

Ставится только из главного потока: `signal.signal` в другом бросает
`ValueError`. Там же исполняется и сам обработчик, поэтому стек меняется и
читается в одном потоке, и замок не нужен — а замок, взятый в обработчике,
пока его держит тот же поток, дал бы взаимоблокировку.
"""

from __future__ import annotations

import signal
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator


class _Wait:
    """Ожидание в стеке. Своя обёртка — чтобы снимать по идентичности, даже если
    два ожидания передали один и тот же колбэк."""

    __slots__ = ("on_sigint",)

    def __init__(self, on_sigint: Callable[[], None]) -> None:
        self.on_sigint = on_sigint


_waits: list[_Wait] = []
#: Обработчик, стоявший до первого ожидания; вернётся после последнего.
_before: Any = None


def _dispatch(signum: int, frame: Any) -> None:
    if _waits:
        _waits[-1].on_sigint()
        return
    # Ожиданий нет, а обработчик ещё наш — так быть не должно. Ctrl+C молча не
    # глотаем: ведём себя как Python по умолчанию.
    signal.default_int_handler(signum, frame)


@contextmanager
def intercept_sigint(on_sigint: Callable[[], None]) -> Iterator[bool]:
    """Пока блок открыт, Ctrl+C зовёт ``on_sigint`` — если это ожидание верхнее.

    Отдаёт ``True``, если перехват стоит, и ``False``, если поставить его нельзя
    (не главный поток, платформа без сигналов): тогда Ctrl+C работает как обычно.
    """
    global _before
    if threading.current_thread() is not threading.main_thread():
        yield False
        return
    if not _waits:
        try:
            previous = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, _dispatch)
        except (ValueError, OSError, AttributeError):
            yield False
            return
        _before = previous
    wait = _Wait(on_sigint)
    _waits.append(wait)
    try:
        yield True
    finally:
        for index in range(len(_waits) - 1, -1, -1):
            if _waits[index] is wait:
                del _waits[index]
                break
        if not _waits:
            try:
                signal.signal(signal.SIGINT, _before)
            except (ValueError, OSError, TypeError):
                pass
            _before = None


__all__ = ["intercept_sigint"]
