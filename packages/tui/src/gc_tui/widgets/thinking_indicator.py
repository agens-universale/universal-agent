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

У строки-итога есть правый сегмент — сводка правок хода («изменено 3 файла ·
+120 −15»), и он КЛИКАБЕЛЕН: нажатие открывает экран изменений сразу в
источнике «Ход». Кликает только сам сегмент, поэтому `compose_status_line`
отдаёт вместе со строкой его x-диапазон: по цельному `Text` координаты первой
клетки сегмента не восстановить, а «клик по всей строке» открывал бы экран от
нажатия мимо цифр.
"""

from __future__ import annotations

import random
import textwrap
import time

from rich.text import Text
from textual import events
from textual.timer import Timer
from textual.widgets import Static

from gc_tui import textual_compat
from gc_tui.messages import IndicatorRowsChanged, OpenEditsScreenRequest

# `_ACCENT` здесь — янтарный цвет РАБОТЫ (RUN_COLOR), не `$accent` наведения.
from gc_tui.palette import DEEP_DIM as _DIM
from gc_tui.palette import GLOW_SHADES
from gc_tui.palette import PROCESSING_SHADES as _PROCESSING_SHADES
from gc_tui.palette import RUN_COLOR as _ACCENT
from gc_tui.palette import VALIDATING_SHADES as _VALIDATING_SHADES
from gc_tui.services.duration_format import format_compact_int, format_duration
from gc_tui.services.trace_glyphs import blink_on
from gc_tui.services.turn_outcome import (
    OUTCOMES,
    compose_status_line,
    format_turn_summary,
    turn_head,
)

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
_STATE_DONE = "done"
_STATE_CANCELLED = "cancelled"
# Ход прервался вместе с процессом (мета «running» без живого хода).
_STATE_INTERRUPTED = "interrupted"
# Стоп запрошен, ход ещё дорабатывает: мигающий глиф стопа без текста
# (решение 26). Не итог — анимация идёт.
_STATE_STOPPING = "stopping"
_STATE_FAILED = "failed"
_STATE_STOPPED_STEPS = "stopped_steps"
_STATE_STOPPED_ERRORS = "stopped_errors"
_STATE_STOPPED_TOKENS = "stopped_tokens"
_STATE_STOPPED_TOOL_CALLS = "stopped_tool_calls"
# Режим auto: проверка действий недоступна, спорные действия выполнить нельзя.
_STATE_STOPPED_JUDGE = "stopped_judge"

# Исходы, которые персистентны как строка-итог (done / cancelled / …) до
# следующего user-send. Единый список для poll и resume; meta.last_indicator
# исхода не несёт — только сводку правок (`edits`).
TERMINAL_INDICATOR_STATES = frozenset(
    {
        _STATE_DONE,
        _STATE_CANCELLED,
        _STATE_INTERRUPTED,
        _STATE_FAILED,
        _STATE_STOPPED_STEPS,
        _STATE_STOPPED_ERRORS,
        _STATE_STOPPED_TOKENS,
        _STATE_STOPPED_TOOL_CALLS,
        _STATE_STOPPED_JUDGE,
    }
)


def is_terminal_indicator_state(state: str) -> bool:
    return state in TERMINAL_INDICATOR_STATES


# Ширина, когда измерить нельзя (виджет вне DOM): та же страховка, что у
# адаптивного футера.
_FALLBACK_WIDTH = 80
#: Причина ошибки переносится не больше чем на столько строк.
ERROR_MAX_ROWS = 3
_STOP_GLYPH = OUTCOMES["cancelled"].glyph


class ThinkingIndicator(Static):
    """Индикатор активности агента (thinking / validating / compressing).

    `started_at` — момент, от которого считается живой счёт: старт хода по
    часам ядра (экран обновляет его на каждом тике, `set_started_at`).
    Если не передан — используется текущее время (виджет монтируется «сейчас»).
    `state` — стартовый режим ("thinking" / "validating" / "compressing").
    Строка-итог — голова `turn_head`: длительность (`frozen_elapsed`) и время
    конца (`ended_at`) приходят снаружи, из записи итога хода; своим счётом
    индикатор длительность хода не подменяет.
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
        frozen_elapsed: float | None = None,
        turn_summary: tuple[int, int, int] | None = None,
        plan: bool = False,
        ended_at: str | None = None,
        error: str | None = None,
    ) -> None:
        self._clock = clock
        # Причина упавшего хода: строка `└` под «✕ Ошибка».
        self._error = error
        # Ход кончился предложенным планом: «План готов», не «Выполнил».
        self._plan = plan
        # Конец хода (UTC ISO из записи): время на часах в строке-итоге.
        self._ended_at = ended_at
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
        self._frozen_elapsed: float | None = frozen_elapsed
        self._turn_summary: str | None = (
            format_turn_summary(*turn_summary) if turn_summary else None
        )
        # Диапазон сегмента сводки — след последней отрисовки: только по нему
        # мышь отличает клик по сводке от клика по строке-итогу.
        self._summary_span: tuple[int, int] | None = None
        self._summary_hover = False
        # Жест начался НА сегменте: клик засчитывается только целиком, от
        # нажатия до отпускания (см. `on_mouse_down`).
        self._pressed_in_summary = False
        self._rotator = PhraseRotator(THINKING_PHRASES)
        # Таймер анимации; None до монтирования.
        self._anim_timer: Timer | None = None
        super().__init__(
            self._render_text(), classes="thinking-indicator", markup=False
        )
        self._sync_error_class()

    @property
    def state(self) -> str:
        """Текущее состояние строки (живое, мигание стопа или итог)."""
        return self._state

    @property
    def has_error_line(self) -> bool:
        """Под строкой-итогом есть строка причины ошибки (вторая строка)."""
        return self._state == _STATE_FAILED and bool(self._error)

    def set_error(self, error: str | None) -> None:
        """Причина упавшего хода — строка `└` под «✕ Ошибка»."""
        if error == self._error:
            return
        self._error = error
        self._sync_error_class()
        if self.is_mounted:
            self.refresh(layout=True)

    def _error_texts(self) -> list[str]:
        """Причина по строкам колонки текста (под `└ `): до трёх строк,
        дальше — обрезка `…` в последней."""
        from gc_tui.services.trace_render import consequence_lead

        col = max(1, self._status_width() - consequence_lead(0).cell_len)
        rows = textwrap.wrap(
            self._error or "",
            col,
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        if len(rows) <= ERROR_MAX_ROWS:
            return rows
        last = rows[ERROR_MAX_ROWS - 1]
        return rows[: ERROR_MAX_ROWS - 1] + [last[: col - 1].rstrip() + "…"]

    @property
    def error_rows(self) -> int:
        """Сколько строк занимает причина (0 — её нет)."""
        return len(self._error_texts()) if self.has_error_line else 0

    def _sync_error_class(self) -> None:
        self.set_class(self.has_error_line, "with-error")
        self.styles.height = 1 + self.error_rows

    def on_resize(self, event: events.Resize) -> None:
        """Ширина изменилась — причина переносится иначе: высота и резерв
        бара следуют за числом строк."""
        before = self.styles.height
        self._sync_error_class()
        if self.has_error_line and self.styles.height != before:
            self.post_message(IndicatorRowsChanged())

    def set_plan(self, plan: bool) -> None:
        """Успешный ход кончился планом: «План готов» вместо «Выполнил»."""
        if plan == self._plan:
            return
        self._plan = plan
        if self.is_mounted:
            self.refresh()

    def set_ended_at(self, ended_at: str | None) -> None:
        """Конец хода из записи итога — время на часах в строке-итоге."""
        if ended_at == self._ended_at:
            return
        self._ended_at = ended_at
        if self.is_mounted:
            self.refresh()

    def set_started_at(self, started_at: float | None) -> None:
        """Старт живого счёта: часы хода ядра, без ожидания человека;
        None — часов ещё нет, счёт не показывается (иначе счёт от отправки
        откатился бы к нулю, когда они появятся)."""
        self._started_at = started_at

    def set_turn_summary(self, summary: tuple[int, int, int] | None) -> None:
        """Сводка правок завершившегося хода — правый сегмент строки-итога."""
        self._turn_summary = format_turn_summary(*summary) if summary else None
        if self.is_mounted:
            self.refresh()

    def set_state(self, state: str) -> None:
        """Переключить визуал без размонтирования. Идемпотентен."""
        if state not in (
            _STATE_THINKING,
            _STATE_VALIDATING,
            _STATE_COMPRESSING,
            _STATE_PROCESSING,
            _STATE_DONE,
            _STATE_CANCELLED,
            _STATE_STOPPING,
            _STATE_INTERRUPTED,
            _STATE_FAILED,
            _STATE_STOPPED_STEPS,
            _STATE_STOPPED_ERRORS,
            _STATE_STOPPED_TOKENS,
            _STATE_STOPPED_TOOL_CALLS,
            _STATE_STOPPED_JUDGE,
        ):
            return
        if self._state == state:
            return
        self._state = state
        self._blink_frame = 0
        self._sync_error_class()
        self._sync_anim()
        if self.is_mounted:
            self.refresh()

    def freeze_elapsed(self, seconds: float | None) -> None:
        """Длительность хода из записи итога: строка «Выполнил за N» берёт
        её, а не свой счёт; None — без «за N» (записи нет)."""
        if seconds == self._frozen_elapsed:
            return
        self._frozen_elapsed = seconds
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
        """Таймер тикает, только пока кадр виден и нужен.

        Итог хода от кадра не зависит, скрытую строку никто не видит.
        """
        if self._anim_timer is None:
            return
        if self._anim_idle():
            self._anim_timer.pause()
        else:
            self._anim_timer.resume()

    def _anim_idle(self) -> bool:
        return self.has_class("hidden") or is_terminal_indicator_state(
            self._state
        )

    def on_mount(self) -> None:
        # Перерисовка 10 раз/сек: глиф держится FRAME_HOLD тиков (≈1.2с цикл),
        # свечение «дышит» каждый тик (1.0с на ping-pong рампы).
        # Созданный сразу конечным (восстановление) или скрытым стартует
        # на паузе.
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

    def _error_line(self) -> Text:
        """`└ <класс>: <текст>` — следствие уровня 0, `DIM`; перенос до
        трёх строк, продолжение — под колонкой текста, не под `└`."""
        from gc_tui.palette import DIM
        from gc_tui.services.trace_render import consequence_lead

        lead = consequence_lead(0)
        indent = " " * lead.cell_len
        line = Text()
        for i, row in enumerate(self._error_texts()):
            if i:
                line.append("\n" + indent)
            else:
                line.append_text(lead)
            line.append(row, style=DIM)
        return line

    def _processing_shade(self) -> str:
        """Цвет «дыхания» точки • «сверяюсь…»: тёмно-серая рампа, кадр = тик."""
        return _PROCESSING_SHADES[self._frame % len(_PROCESSING_SHADES)]

    def _validating_shade(self) -> str:
        """Цвет «дыхания» искры ✦ «завершаю…»: серая рампа, кадр = тик."""
        return _VALIDATING_SHADES[self._frame % len(_VALIDATING_SHADES)]

    def _status_width(self) -> int:
        """Ширина строки для правого сегмента: своя, иначе экрана, иначе страховка.

        `_render_text` зовётся и до монтирования (из `__init__`) — там мерить
        нечего, и обе пробы падают."""
        for probe in (lambda: self.size.width, lambda: self.app.size.width):
            try:
                width = int(probe())
            except Exception:
                width = 0
            if width > 0:
                return width
        return _FALLBACK_WIDTH

    # ---- сегмент сводки: единственная кликабельная часть строки ----------

    def _in_summary(self, event: events.MouseEvent) -> bool:
        """Стоит ли указатель на сегменте сводки.

        Сверка идёт по ЭКРАННОЙ координате и области контента: `event.x`
        отсчитывается от края региона виджета вместе с паддингом, а диапазон
        сегмента живёт в координатах содержимого — при ненулевом левом
        паддинге шкалы разъехались бы.
        """
        span = self._summary_span
        if span is None or event.screen_y != self.content_region.y:
            return False
        x = event.screen_x - self.content_region.x
        return span[0] <= x < span[1]

    def _set_summary_hover(self, hover: bool) -> None:
        if hover == self._summary_hover:
            return
        self._summary_hover = hover
        if self.is_mounted:
            self.refresh()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        """Запомнить, что жест НАЧАЛСЯ на сегменте.

        Строка-итог — обычный текст, её выделяют мышью. Протяжка выделения от
        начала строки с отпусканием на сводке даёт `Click` ровно там же, где
        и настоящее нажатие: отличить их можно только по началу жеста.
        """
        self._pressed_in_summary = event.button == 1 and self._in_summary(
            event
        )

    def on_click(self, event: events.Click) -> None:
        """Клик по сводке — экран изменений сразу в источнике «Ход».

        Засчитывается ОДИН короткий жест левой кнопкой, начатый и отпущенный
        на сегменте. Каждое из условий закрывает свой живой промах:

        * `chain != 1` — двойной клик. Сообщение всплывает несколько тактов, и
          второй `Click` успевает прийти в тот же виджет: экран открывался
          дважды, Esc «не закрывал» его. Клик-модель базы (`click_model`) вторым
          кликом ничего и не открывает;
        * `button != 1` — правая кнопка: у неё в интерфейсе своей семантики нет,
          и открывать ею экран пользователь не просил;
        * `_pressed_in_summary` — протяжка выделения, отпущенная на сводке.
        """
        pressed, self._pressed_in_summary = self._pressed_in_summary, False
        if event.button != 1 or event.chain != 1:
            return
        if not pressed or not self._in_summary(event):
            return
        event.stop()
        # Импорт локальный: `edits_view` тянет за собой весь слой `agent_edits`,
        # а виджет живёт в каждом кадре трейса — грузить это ради одной
        # константы на импорте модуля незачем (`main.py` его тоже импортирует
        # лениво).
        from gc_tui.services.edits_view import SOURCE_TURN

        self.post_message(OpenEditsScreenRequest(SOURCE_TURN))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        self._set_summary_hover(self._in_summary(event))

    def on_leave(self, _event: events.Leave) -> None:
        # Указатель ушёл с виджета — начатый на сегменте жест закончится уже не
        # здесь, и засчитывать его по возвращении нельзя.
        self._pressed_in_summary = False
        self._set_summary_hover(False)

    # ---- отрисовка -------------------------------------------------------

    def render(self) -> Text:
        return self._render_text()

    def _render_outcome(self) -> Text:
        """Строка-итог: голова из словаря (`turn_head`), справа — сводка
        правок хода, под «✕ Ошибка» — причина."""
        head = turn_head(
            self._state,
            plan=self._plan,
            duration_sec=self._frozen_elapsed,
            ended_at=self._ended_at,
        )
        head.stylize(_DIM)
        # Сводка правок — у ЛЮБОГО исхода: упавший или остановленный ход
        # файлы уже менял, и после ошибки знать это важнее всего.
        line, self._summary_span = compose_status_line(
            head,
            self._turn_summary,
            self._status_width(),
            hover=self._summary_hover,
        )
        if self.has_error_line:
            line.append("\n")
            line.append_text(self._error_line())
        return line

    def _render_text(self) -> Text:
        text = Text()
        # Сегмент рисует только строка-итог «Выполнил»; во всех прочих
        # состояниях его нет, и след прошлой отрисовки обязан пропасть вместе
        # с ним — иначе клик попал бы в пустоту на месте старых цифр.
        self._summary_span = None
        if self._state == _STATE_STOPPING:
            # Мигает исчезая: в паузе — пустая клетка той же ширины.
            on = blink_on(self._blink_frame)
            text.append(_STOP_GLYPH if on else " ", style=_DIM)
            return text
        if is_terminal_indicator_state(self._state):
            return self._render_outcome()
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
            elapsed = int(self._clock() - self._started_at)
            parts.append(format_duration(elapsed))
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
