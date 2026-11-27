"""Индикатор открытия сессии и превью (спека шага 2 окна ленты, §3.3):
дышащая точка в одну клетку и надпись с бликом — тот же язык, что у
индикатора хода (точка «сверяюсь…», блик `services.shimmer`).

Появляется, только если ожидание дольше `OPEN_INDICATOR_DELAY`: быстрое
открытие ничего не мигает. Место строки — у экрана, где нажали (карточка
списка, строка подсказок превью): индикатор отдаёт ему кадр через
`paint`, а по остановке — `None` (вернуть строку как была).
"""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual.timer import Timer
from textual.widget import Widget

from gc_tui.palette import MCP_SHIMMER_SHADES, PROCESSING_SHADES
from gc_tui.services.shimmer import shimmer_pass, shimmer_text

OPEN_INDICATOR_DELAY = 0.5
_TICK = 0.1


def open_indicator_text(label: str, frame: int) -> Text:
    """`• открываю сессию`: точка дышит, надпись с бликом; кадр — тик."""
    text = Text(no_wrap=True)
    text.append("•", style=PROCESSING_SHADES[frame % len(PROCESSING_SHADES)])
    text.append(" ")
    step = frame % shimmer_pass(label, MCP_SHIMMER_SHADES)
    text.append_text(shimmer_text(label, step, MCP_SHIMMER_SHADES))
    return text


class OpenIndicator:
    """Кадры индикатора на таймерах `host`; рисует `paint` хозяина."""

    def __init__(
        self,
        label: str,
        *,
        host: Widget,
        paint: Callable[[Text | None], None],
    ) -> None:
        self._label = label
        self._host = host
        self._paint = paint
        self._frame = 0
        self._shown = False
        self._timers: list[Timer] = [
            host.set_timer(OPEN_INDICATOR_DELAY, self._show)
        ]

    @property
    def shown(self) -> bool:
        return self._shown

    def stop(self) -> None:
        """Ожидание кончилось или отменено: строка — как была."""
        for timer in self._timers:
            timer.stop()
        self._timers = []
        if self._shown:
            self._shown = False
            self._paint(None)

    def _show(self) -> None:
        self._shown = True
        self._timers.append(self._host.set_interval(_TICK, self._tick))
        self._paint(open_indicator_text(self._label, self._frame))

    def _tick(self) -> None:
        self._frame += 1
        self._paint(open_indicator_text(self._label, self._frame))
