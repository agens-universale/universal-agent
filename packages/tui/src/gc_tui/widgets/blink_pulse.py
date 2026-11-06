"""Мигание `●` у идущих строк ленты: общий таймер-тик."""

from __future__ import annotations

from gc_tui import textual_compat
from gc_tui.services.trace_glyphs import BLINK_TICK_S


class BlinkPulseMixin:
    """Таймер мигания есть только у запущенной строки с `_blinking`.

    Хозяин держит `_pulse_frame` / `_pulse_timer`, свойство `_blinking`
    и `_redraw_pulse()` — перерисовку глифа без layout.
    """

    def _sync_pulse(self) -> None:
        """Идемпотентно: заводит или гасит тик по текущему состоянию."""
        if self._blinking and self.is_running:
            if self._pulse_timer is None:
                self._pulse_timer = self.set_interval(
                    BLINK_TICK_S, self._pulse_tick
                )
        elif self._pulse_timer is not None:
            self._pulse_timer.stop()
            self._pulse_timer = None

    def _pulse_tick(self) -> None:
        # Freeze UI на время app-select (Screen._selecting) — не тикаем.
        try:
            if textual_compat.is_selecting(self.screen):
                return
        except Exception:
            pass
        if not self._blinking:
            return
        self._pulse_frame += 1
        self._redraw_pulse()
