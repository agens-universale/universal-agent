"""Мигание `●` у идущих строк ленты: общий таймер-тик."""

from __future__ import annotations

from gc_tui import textual_compat
from gc_tui.services.trace_glyphs import BLINK_TICK_S
from gc_tui.widgets.missed_frame import miss_frame


class BlinkPulseMixin:
    """Таймер мигания есть только у запущенной строки с `_blinking`.

    Хозяин держит `_pulse_frame` / `_pulse_timer` / `_frame_missed`,
    свойства `_blinking` и `_pulse_widget` (перерисовываемый глиф; до
    `compose` — `None`) и `_redraw_pulse()` — перерисовку глифа без
    layout. Глиф не виден — кадр растёт, но не рисуется (перерисовка
    невидимого пересобирает карту экрана), тик отмечает пропуск
    (`miss_frame`).
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
        glyph = self._pulse_widget
        if glyph is not None and not textual_compat.is_visible(glyph):
            miss_frame(self)
            return
        self._redraw_pulse()
