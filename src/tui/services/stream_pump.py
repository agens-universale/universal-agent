"""StreamPump — событийная коалесценс-перерисовка стрима (TUI).

Оборачивает on_stream_delta: кладёт дельту в StreamSink (как раньше) и не чаще
раза в `throttle_s` дёргает signal() для перерисовки UI. Троттл-состояние живёт
на worker-потоке (агент-поток) — один поток, без локов; StreamSink уже
thread-safe. Презентация, не бизнес-логика."""
from __future__ import annotations

import time
from typing import Any, Callable

STREAM_REFRESH_MS = 350   # потолок коалесценса событийной перерисовки текста стрима


class StreamPump:
    def __init__(self, sink: Any, signal: Callable[[], None], *,
                 throttle_s: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._sink = sink
        self._signal = signal
        self._throttle_s = throttle_s
        self._clock = clock
        self._last = clock() - throttle_s   # первая дельта сразу сигналит

    def on_delta(self, kind: str, text: str) -> None:
        self._sink.push(kind, text)
        now = self._clock()
        if now - self._last >= self._throttle_s:
            self._last = now
            try:
                self._signal()
            except Exception:
                pass   # поздний сигнал при teardown loop'а не должен ронять worker
