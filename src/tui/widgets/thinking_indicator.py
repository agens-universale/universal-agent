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

# Дыхание «обрабатываю…» (фоновая доделка): одна точка плавно растёт и опадает
# ·→∙→•→∙, без яркого морфа и без цвета — отличает фоновую доделку от активной
# генерации «думаю…».
PULSE_FRAMES = ["·", "∙", "•", "∙"]
PULSE_HOLD = 3   # кадр держится 3 тика (0.3с) → полный цикл ≈ 1.2с

_STATE_THINKING = "thinking"
_STATE_VALIDATING = "validating"
_STATE_COMPRESSING = "compressing"
_STATE_PROCESSING = "processing"


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
    ) -> None:
        self._clock = clock
        self._sink = sink
        self._reasoning_tag = reasoning_tag
        self._started_at: float = (
            started_at if started_at is not None else clock()
        )
        self._frame: int = 0
        self._state: str = state
        self._rotator = PhraseRotator(THINKING_PHRASES)
        super().__init__(self._render_text(), classes="thinking-indicator", markup=False)

    def set_state(self, state: str) -> None:
        """Переключить визуал без размонтирования. Идемпотентен."""
        if state not in (_STATE_THINKING, _STATE_VALIDATING, _STATE_COMPRESSING, _STATE_PROCESSING):
            return
        if self._state == state:
            return
        self._state = state
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

    def _pulse_glyph(self) -> str:
        """Глиф дыхания «обрабатываю…»: ·∙•∙, кадр держится PULSE_HOLD тиков."""
        return PULSE_FRAMES[(self._frame // PULSE_HOLD) % len(PULSE_FRAMES)]

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
            glyph, color = self._anim()
            text.append(glyph, style=color)
            text.append(" ")
            text.append("сжимаю контекст…", style=_ACCENT)
        elif self._state == _STATE_PROCESSING:
            # Ответ показан, прогон ещё доделывает (finish-gate / баунс / доделка
            # без финалайзера): тихое дыхание точкой ·∙•∙ + слово серым — фоновая
            # доделка, не активная генерация «думаю…». Всё dim, без цвета.
            text.append(self._pulse_glyph(), style=_DIM)
            text.append(" ")
            text.append("обрабатываю…", style=_DIM)
        else:
            glyph, color = self._anim()
            text.append(glyph, style=color)
            text.append(" ")
            text.append(f"{self._rotator.current()}…", style=_ACCENT)
        elapsed = int(self._clock() - self._started_at)
        # Elapsed — наследует deep-dim из .thinking-indicator (CSS).
        parens = f" ({format_duration(elapsed)}"
        if self._state == _STATE_THINKING and self._sink is not None:
            units = self._sink.progress_units()
            if units > 0:
                parens += f" · ↓ {format_compact_int(units)} токенов"
            _r_state, dur = self._sink.reasoning_state()
            if _r_state == "thinking" and self._reasoning_tag:
                parens += " · размышление"
            elif _r_state == "done":
                parens += f" · обдумал за {format_duration(round(dur))}"
        parens += ")"
        text.append(parens)
        return text
