"""ThinkingIndicator — индикатор активности агента (spec 2026-05-26 + 2026-05-28).

Виджет с тремя состояниями:
- "thinking" — агент работает (LLM-вызов, tools): `⠋ думаю… (Ns)` accent-цвет.
- "validating" — финальный ответ виден, идёт пост-валидация (intent_guard /
  MCP cleanup), не требует реакции юзера: `✓ завершаю… (Ns)` dim-цвет.
- "compressing" — идёт суммаризация истории: `⠋ сжимаю контекст… (Ns)`.

Переключается через `set_state("thinking" | "validating" | "compressing")`
без размонтирования — непрерывный визуал между фазами, без флика
«исчез → вернулся».

Управляется из TraceScreen._poll_active: meta-флаг `tui_summarizing` →
compressing; иначе по `_last_event_kind`: "_finalized" sentinel → validating,
иначе → thinking.
"""
from __future__ import annotations

import random
import time

from rich.text import Text
from textual.widgets import Static

from tui.palette import DEEP_DIM as _DIM, RUN_COLOR as _ACCENT


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


# Классический braille-spin: крутящийся «сегмент». Все кадры — braille
# (narrow, одинаковая ширина) → строка не дёргается. Плавное вращение.
MORPH_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# Рампа «дыхания» цветом (ping-pong уже зашит в список): gold тускло→ярко→тускло.
GLOW_SHADES = [
    "#9c7a4a", "#b8915c", "#d4a574", "#e0b585", "#ecc596",
    "#f0c896", "#ecc596", "#e0b585", "#d4a574", "#b8915c",
]

_STATE_THINKING = "thinking"
_STATE_VALIDATING = "validating"
_STATE_COMPRESSING = "compressing"


def _fmt_units(n: int) -> str:
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


class ThinkingIndicator(Static):
    """Индикатор активности агента (thinking / validating / compressing).

    `started_at` — момент, от которого считается elapsed (обычно
    `_thinking_started_at` TraceScreen — момент последнего user-send).
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
    ) -> None:
        self._clock = clock
        self._sink = sink
        self._started_at: float = (
            started_at if started_at is not None else clock()
        )
        self._frame: int = 0
        self._state: str = state
        self._rotator = PhraseRotator(THINKING_PHRASES)
        super().__init__(self._render_text(), classes="thinking-indicator", markup=False)

    def set_state(self, state: str) -> None:
        """Переключить визуал без размонтирования. Идемпотентен."""
        if state not in (_STATE_THINKING, _STATE_VALIDATING, _STATE_COMPRESSING):
            return
        if self._state == state:
            return
        self._state = state
        if self.is_mounted:
            self.refresh()

    def on_mount(self) -> None:
        # Перерисовка 10 раз/сек — braille-spin из 10 кадров (1.0с на оборот).
        self.set_interval(0.1, self._tick)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(MORPH_FRAMES)
        # render() читает _frame динамически — достаточно запросить refresh.
        # Guard `is_mounted` — в юнит-тестах _tick дёргается без app-контекста.
        if self.is_mounted:
            self.refresh()

    def render(self) -> Text:
        return self._render_text()

    def _render_text(self) -> Text:
        text = Text()
        if self._state == _STATE_VALIDATING:
            # Финальный ответ виден; статичный ✓ завершаю… (dim), без анимации —
            # чтобы НЕ соревноваться вниманием с уже отрисованным task_report.
            text.append("✓", style=_DIM)
            text.append(" ")
            text.append("завершаю…", style=_DIM)
        elif self._state == _STATE_COMPRESSING:
            # Идёт суммаризация истории: анимированный глиф + фикс-текст (не из
            # rotator — это конкретная операция, не «общее размышление»).
            glyph = MORPH_FRAMES[self._frame % len(MORPH_FRAMES)]
            color = GLOW_SHADES[self._frame % len(GLOW_SHADES)]
            text.append(glyph, style=color)
            text.append(" ")
            text.append("сжимаю контекст…", style=_ACCENT)
        else:
            glyph = MORPH_FRAMES[self._frame % len(MORPH_FRAMES)]
            color = GLOW_SHADES[self._frame % len(GLOW_SHADES)]
            text.append(glyph, style=color)
            text.append(" ")
            text.append(f"{self._rotator.current()}…", style=_ACCENT)
        elapsed = int(self._clock() - self._started_at)
        # Elapsed — наследует deep-dim из .thinking-indicator (CSS).
        parens = f" ({elapsed}с"
        if self._state == _STATE_THINKING and self._sink is not None:
            units = self._sink.progress_units()
            if units > 0:
                parens += f" · ↓ {_fmt_units(units)} токенов"
            r_state, dur = self._sink.reasoning_state()
            if r_state == "thinking":
                parens += " · размышление"
            elif r_state == "done":
                parens += f" · обдумал за {round(dur)}с"
        parens += ")"
        text.append(parens)
        return text
