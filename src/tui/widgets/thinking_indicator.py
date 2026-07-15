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
"""
from __future__ import annotations

import random
import time

from rich.text import Text
from textual.widgets import Static

from tui.palette import DEEP_DIM as _DIM, RUN_COLOR as _ACCENT
from tui.services.duration_format import format_compact_int, format_duration


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

# Рампа «дыхания» цветом (ping-pong уже зашит в список): gold тускло→ярко→тускло.
GLOW_SHADES = [
    "#9c7a4a", "#b8915c", "#d4a574", "#e0b585", "#ecc596",
    "#f0c896", "#ecc596", "#e0b585", "#d4a574", "#b8915c",
]

# Период счётчика кадров: общий кратный темпа глифа и рампы цвета — счётчик
# остаётся ограниченным, а обе под-анимации выходят на старт синхронно.
_ANIM_PERIOD = FRAME_HOLD * len(MORPH_FRAMES) * len(GLOW_SHADES)

# «Дыхание» точки «сверяюсь…» (судья сверяет ответ перед закрытием хода): одна
# точка • статична по форме, её ЦВЕТ плавно тускнеет↔светлеет по ТЁМНО-серой
# рампе — та же техника дыхания яркостью, что у ✦/▸, но в самых глубоких серых
# (тише всего: лесенка громкости ▸ > ✦ > •). Плавно на 0.1с-тике, без морфа формы.
_PROCESSING_SHADES = [
    "#4c525b", "#545b65", "#5d646e", "#666e78", "#6f7782",
    "#7a8290", "#6f7782", "#666e78", "#5d646e", "#545b65",
]

# «Дыхание» искры «завершаю…»: ✦ статична по форме, цвет плавно тускнеет↔
# светлеет (серая рампа, ping-pong, ~1.0с) — спокойная финализация. Та же
# техника, что пульс running-глифа ▸, но в приглушённых серых.
_VALIDATING_SHADES = [
    "#5e6675", "#69727f", "#747e8b", "#808c99", "#8c98a6",
    "#9aa4b2", "#8c98a6", "#808c99", "#747e8b", "#69727f",
]

_STATE_THINKING = "thinking"
_STATE_VALIDATING = "validating"
_STATE_COMPRESSING = "compressing"
_STATE_PROCESSING = "processing"
_STATE_DONE = "done"
_STATE_CANCELLED = "cancelled"
_STATE_FAILED = "failed"
_STATE_STOPPED_STEPS = "stopped_steps"
_STATE_STOPPED_ERRORS = "stopped_errors"

# Исходы, которые персистентны как строка-итог (done / cancelled / …) до
# следующего user-send. Единый список для poll, resume и meta.last_indicator.
TERMINAL_INDICATOR_STATES = frozenset({
    _STATE_DONE,
    _STATE_CANCELLED,
    _STATE_FAILED,
    _STATE_STOPPED_STEPS,
    _STATE_STOPPED_ERRORS,
})


def is_terminal_indicator_state(state: str) -> bool:
    return state in TERMINAL_INDICATOR_STATES


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
        reasoning_tag: bool = False,
        reasoning_clock=None,
        frozen_elapsed: int | None = None,
    ) -> None:
        self._clock = clock
        self._sink = sink
        self._reasoning_tag = reasoning_tag
        self._reasoning_clock = reasoning_clock
        self._started_at: float = (
            started_at if started_at is not None else clock()
        )
        self._frame: int = 0
        self._state: str = state
        self._frozen_elapsed: int | None = frozen_elapsed
        self._rotator = PhraseRotator(THINKING_PHRASES)
        super().__init__(self._render_text(), classes="thinking-indicator", markup=False)

    def set_state(self, state: str) -> None:
        """Переключить визуал без размонтирования. Идемпотентен."""
        if state not in (_STATE_THINKING, _STATE_VALIDATING, _STATE_COMPRESSING,
                         _STATE_PROCESSING, _STATE_DONE, _STATE_CANCELLED, _STATE_FAILED,
                         _STATE_STOPPED_STEPS, _STATE_STOPPED_ERRORS):
            return
        if self._state == state:
            return
        self._state = state
        if state == _STATE_DONE and self._frozen_elapsed is None:
            self._frozen_elapsed = int(self._clock() - self._started_at)
        if self.is_mounted:
            self.refresh()

    def on_mount(self) -> None:
        # Перерисовка 10 раз/сек: глиф держится FRAME_HOLD тиков (≈1.2с цикл),
        # свечение «дышит» каждый тик (1.0с на ping-pong рампы).
        self.set_interval(0.1, self._tick)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % _ANIM_PERIOD
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

    def render(self) -> Text:
        return self._render_text()

    def _render_text(self) -> Text:
        text = Text()
        if self._state == _STATE_DONE:
            text.append("✓", style=_DIM)
            text.append(" ")
            # Живой done всегда замораживает elapsed в set_state → «Выполнил за N».
            # frozen_elapsed=None — восстановление старой сессии без сохранённого
            # времени → «Выполнил» без «за N» (не выдумываем 0с).
            if self._frozen_elapsed is not None:
                text.append(f"Выполнил за {format_duration(self._frozen_elapsed)}", style=_DIM)
            else:
                text.append("Выполнил", style=_DIM)
            return text
        if self._state == _STATE_CANCELLED:
            text.append("■", style=_DIM)
            text.append(" ")
            text.append("Отменено", style=_DIM)
            return text
        if self._state == _STATE_FAILED:
            text.append("✗", style=_DIM)
            text.append(" ")
            text.append("Ошибка", style=_DIM)
            return text
        if self._state == _STATE_STOPPED_STEPS:
            text.append("⏹", style=_DIM)
            text.append(" ")
            text.append("Остановлено: превышен лимит шагов", style=_DIM)
            return text
        if self._state == _STATE_STOPPED_ERRORS:
            text.append("⏹", style=_DIM)
            text.append(" ")
            text.append("Остановлено: превышен лимит ошибок", style=_DIM)
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
        elapsed = int(self._clock() - self._started_at)
        parens = f" ({format_duration(elapsed)}"
        if self._state == _STATE_THINKING:
            units = self._sink.progress_units() if self._sink is not None else 0
            if units > 0:
                parens += f" · ↓ {format_compact_int(units)} токенов"
            clk = self._reasoning_clock
            if clk is not None:
                if clk.live() and self._reasoning_tag:
                    parens += " · размышление"
                elif not clk.live():
                    secs = clk.episode()
                    if secs > 0:
                        if round(secs) == 0:
                            parens += " · обдумал"            # <0.5с → без «за 0с»
                        else:
                            parens += f" · обдумал за {format_duration(round(secs))}"
        parens += ")"
        text.append(parens)
        return text
