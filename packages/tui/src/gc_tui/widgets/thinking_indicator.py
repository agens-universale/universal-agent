"""ThinkingIndicator — индикатор активности агента (spec 2026-05-26 + 2026-05-28).

Виджет с тремя состояниями:
- "thinking" — агент работает (LLM-вызов, tools): `⠋ думаю… (Nс)` accent-цвет;
  в этом состоянии при наличии stream-sink добавляется хвост
  `· ↓ N токенов · размышление / обдумал за Nс`.
- "validating" — финальный ответ виден, идёт пост-валидация (intent_guard /
  MCP cleanup), не требует реакции юзера: `✓ завершаю… (Nс)` dim-цвет.
- "compressing" — идёт суммаризация истории: `⠋ сжимаю контекст… (Nс)`.

Переключается через `set_state("thinking" | "validating" | "compressing")`
без размонтирования — непрерывный визуал между фазами, без флика
«исчез → вернулся».

Управляется из TraceScreen._poll_active: meta-флаг `runtime.summarizing` →
compressing; иначе по `_last_event_kind`: "_finalized" sentinel → validating,
иначе → thinking.

Индикатор показывает только идущую работу (S10 §3.3). Исход хода — строка
итога в ленте (`widgets.turn_line.TurnLine`), не состояние индикатора.
"""

from __future__ import annotations

import random
import time

from rich.text import Text
from textual.timer import Timer
from textual.widgets import Static

from gc_tui import textual_compat

# `_ACCENT` здесь — янтарный цвет РАБОТЫ (RUN_COLOR), не `$accent` наведения.
from gc_tui.palette import DEEP_DIM as _DIM
from gc_tui.palette import GLOW_SHADES
from gc_tui.palette import PROCESSING_SHADES as _PROCESSING_SHADES
from gc_tui.palette import RUN_COLOR as _ACCENT
from gc_tui.palette import VALIDATING_SHADES as _VALIDATING_SHADES
from gc_tui.services.duration_format import (
    format_compact_int,
    format_duration,
    format_timer,
)
from gc_tui.services.trace_glyphs import blink_on
from gc_tui.services.turn_outcome import OUTCOMES

THINKING_PHRASES = [
    "думаю",
    "размышляю",
    "соображаю",
    "анализирую",
    "прорабатываю",
    "прикидываю",
    "сопоставляю",
    "обдумываю",
    "копаю глубже",
    "шевелю извилинами",
    "кручу шестерёнки",
    "погружаюсь",
]


class PhraseRotator:
    """Возвращает фразу, меняя её на случайную (не равную текущей) раз в `period`.

    `rng`/`clock` инъектируемы только ради детерминированных тестов.
    Старт — `phrases[0]` (детерминированно).
    """

    def __init__(
        self,
        phrases: list[str],
        *,
        period: float = 3.5,
        rng=random,
        clock=time.monotonic,
    ) -> None:
        self._phrases = phrases
        self._period = period
        self._rng = rng
        self._clock = clock
        self._current = phrases[0]
        self._last_change = clock()

    def current(self) -> str:
        now = self._clock()
        if now - self._last_change >= self._period:
            self._last_change = now
            choices = [p for p in self._phrases if p != self._current]
            if choices:
                self._current = self._rng.choice(choices)
        return self._current


# Компас → искра: уголёк (·) → вращение треугольника против часовой 1,5 оборота
# (▴◂▾▸ + ▴◂: вверх→влево→вниз→вправо…, «обрабатываю») → искра вспыхивает (✦),
# гаснет в полую (✧) и оседает крестом (+). Оживляем фирменную искру ✦ (та же,
# что в заголовке). Треугольники — ambiguous-width: в большинстве терминалов
# 1 ячейка, внутри цикла ширина консистентна; на стыке с narrow-глифами в
# CJK-режиме возможен сдвиг на 1 клетку (статичный, не дёрганье между кадрами).
MORPH_FRAMES = ["·", "▴", "◂", "▾", "▸", "▴", "◂", "✦", "✧", "+"]

# Каждый кадр держится FRAME_HOLD тиков (тик = 0.1с) → 10 кадров × 0.2с ≈ 2.0с на
# полный цикл. Цвет (GLOW_SHADES) при этом меняется каждый тик — глиф и свечение
# раскадрованы независимо (списки разной длины), см. _anim().
FRAME_HOLD = 2

# Период счётчика кадров: общий кратный темпа глифа и рампы цвета — счётчик
# остаётся ограниченным, а обе под-анимации выходят на старт синхронно.
_ANIM_PERIOD = FRAME_HOLD * len(MORPH_FRAMES) * len(GLOW_SHADES)

_STATE_THINKING = "thinking"
_STATE_VALIDATING = "validating"
_STATE_COMPRESSING = "compressing"
_STATE_PROCESSING = "processing"
# Стоп запрошен, ход ещё дорабатывает: мигающий глиф стопа без текста
# (решение 26). Не итог — анимация идёт.
_STATE_STOPPING = "stopping"
# Белый список `set_state`: только работа (S10 §3.3).
_WORK_STATES = frozenset(
    {
        _STATE_THINKING,
        _STATE_VALIDATING,
        _STATE_COMPRESSING,
        _STATE_PROCESSING,
        _STATE_STOPPING,
    }
)
_STOP_GLYPH = OUTCOMES["cancelled"].glyph


class ThinkingIndicator(Static):
    """Индикатор активности агента (thinking / validating / compressing).

    `started_at` — момент, от которого считается живой счёт: старт хода по
    часам ядра (экран обновляет его на каждом тике, `set_started_at`).
    Если не передан — используется текущее время (виджет монтируется «сейчас»).
    `state` — стартовый режим ("thinking" / "validating" / "compressing").
    """

    DEFAULT_CSS = ""  # стили — .thinking-indicator в styles.tcss

    def __init__(
        self,
        *,
        started_at: float | None = None,
        state: str = _STATE_THINKING,
        clock=time.monotonic,
        sink=None,
        reasoning_tag: bool = False,
        reasoning_clock=None,
    ) -> None:
        self._clock = clock
        self._sink = sink
        self._reasoning_tag = reasoning_tag
        self._reasoning_clock = reasoning_clock
        # None — счёта нет: часов хода ядра ещё нет (`set_started_at`).
        self._started_at: float | None = (
            started_at if started_at is not None else clock()
        )
        self._frame: int = 0
        # Кадр мигания «останавливаю» — свой счёт, без обёртки цикла дыхания.
        self._blink_frame: int = 0
        self._state: str = state
        self._rotator = PhraseRotator(THINKING_PHRASES)
        # Таймер анимации; None до монтирования.
        self._anim_timer: Timer | None = None
        super().__init__(
            self._render_text(), classes="thinking-indicator", markup=False
        )

    @property
    def state(self) -> str:
        """Текущее состояние строки (живая работа или мигание стопа)."""
        return self._state

    def set_started_at(self, started_at: float | None) -> None:
        """Старт живого счёта: часы хода ядра, без ожидания человека;
        None — часов ещё нет, счёт не показывается (иначе счёт от отправки
        откатился бы к нулю, когда они появятся)."""
        self._started_at = started_at

    def set_state(self, state: str) -> None:
        """Переключить визуал без размонтирования. Идемпотентен."""
        if state not in _WORK_STATES:
            return  # исход хода — строка в ленте, не индикатор
        if self._state == state:
            return
        self._state = state
        self._blink_frame = 0
        self._sync_anim()
        if self.is_mounted:
            self.refresh()

    def hide(self) -> None:
        """Скрыть строку (агент ждёт ответа человека); анимация стоит."""
        self.add_class("hidden")
        self._sync_anim()

    def show(self, state: str) -> None:
        """Вернуть скрытую строку в состоянии ``state``."""
        self.remove_class("hidden")
        self.set_state(state)
        self._sync_anim()

    def _sync_anim(self) -> None:
        """Таймер тикает, только пока кадр виден: скрытую строку никто не
        видит."""
        if self._anim_timer is None:
            return
        if self._anim_idle():
            self._anim_timer.pause()
        else:
            self._anim_timer.resume()

    def _anim_idle(self) -> bool:
        return self.has_class("hidden")

    def on_mount(self) -> None:
        # Перерисовка 10 раз/сек: глиф держится FRAME_HOLD тиков (≈1.2с цикл),
        # свечение «дышит» каждый тик (1.0с на ping-pong рампы).
        # Созданный скрытым стартует на паузе.
        self._anim_timer = self.set_interval(
            0.1, self._tick, pause=self._anim_idle()
        )

    def _tick(self) -> None:
        # F4: freeze UI на время app-select — не дёргаем анимацию (Screen._selecting).
        try:
            if textual_compat.is_selecting(self.screen):
                return
        except Exception:
            pass
        self._frame = (self._frame + 1) % _ANIM_PERIOD
        self._blink_frame += 1
        # render() читает _frame динамически — достаточно запросить refresh.
        # Guard `is_mounted` — в юнит-тестах _tick дёргается без app-контекста.
        if self.is_mounted:
            self.refresh()

    def _anim(self) -> tuple[str, str]:
        """(глиф, цвет) текущего кадра. Глиф держится FRAME_HOLD тиков, цвет
        меняется каждый тик — раскадрованы независимо (списки разной длины)."""
        glyph = MORPH_FRAMES[(self._frame // FRAME_HOLD) % len(MORPH_FRAMES)]
        color = GLOW_SHADES[self._frame % len(GLOW_SHADES)]
        return glyph, color

    def _processing_shade(self) -> str:
        """Цвет «дыхания» точки • «сверяюсь…»: тёмно-серая рампа, кадр = тик."""
        return _PROCESSING_SHADES[self._frame % len(_PROCESSING_SHADES)]

    def _validating_shade(self) -> str:
        """Цвет «дыхания» искры ✦ «завершаю…»: серая рампа, кадр = тик."""
        return _VALIDATING_SHADES[self._frame % len(_VALIDATING_SHADES)]

    # ---- отрисовка -------------------------------------------------------

    def render(self) -> Text:
        return self._render_text()

    def _render_text(self) -> Text:
        text = Text()
        if self._state == _STATE_STOPPING:
            # Мигает исчезая: в паузе — пустая клетка той же ширины.
            on = blink_on(self._blink_frame)
            text.append(_STOP_GLYPH if on else " ", style=_DIM)
            return text
        if self._state == _STATE_VALIDATING:
            text.append("✦", style=self._validating_shade())
            text.append(" ")
            text.append("завершаю…", style=_DIM)
        elif self._state == _STATE_COMPRESSING:
            glyph, color = self._anim()
            text.append(glyph, style=color)
            text.append(" ")
            text.append("сжимаю контекст…", style=_ACCENT)
        elif self._state == _STATE_PROCESSING:
            text.append("•", style=self._processing_shade())
            text.append(" ")
            text.append("сверяюсь…", style=_DIM)
        else:
            glyph, color = self._anim()
            text.append(glyph, style=color)
            text.append(" ")
            text.append(f"{self._rotator.current()}…", style=_ACCENT)
        parts = self._live_parts()
        if parts:
            text.append(f" ({' · '.join(parts)})")
        return text

    def _live_parts(self) -> list[str]:
        """Скобки живой строки: счёт хода (если часы есть), у «думаю» —
        токены стрима и размышление."""
        parts: list[str] = []
        if self._started_at is not None:
            timer = format_timer(int(self._clock() - self._started_at))
            if timer is not None:
                parts.append(timer)
        if self._state != _STATE_THINKING:
            return parts
        units = self._sink.progress_units() if self._sink is not None else 0
        if units > 0:
            parts.append(f"↓ {format_compact_int(units)} токенов")
        clk = self._reasoning_clock
        if clk is None:
            return parts
        if clk.live():
            if self._reasoning_tag:
                parts.append("размышление")
            return parts
        secs = clk.episode()
        if secs > 0:
            # <0.5с → без «за 0с»
            rounded = round(secs)
            parts.append(
                f"обдумал за {format_duration(rounded)}"
                if rounded
                else "обдумал"
            )
        return parts
