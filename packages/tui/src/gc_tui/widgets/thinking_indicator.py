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
import time

from rich.text import Text
from textual import events
from textual.widgets import Static

from gc_tui.messages import OpenEditsScreenRequest

# `_HOVER` — ровно тот `$accent`, которым база красит наведение на служебные
# элементы блоков (`.diff-more:hover` / `.block-copy:hover` в styles.tcss).
# `_ACCENT` здесь занят янтарным цветом РАБОТЫ (RUN_COLOR) — это другой смысл.
from gc_tui.palette import ACCENT as _HOVER
from gc_tui.palette import DEEP_DIM as _DIM
from gc_tui.palette import RUN_COLOR as _ACCENT
from gc_tui.services.duration_format import format_compact_int, format_duration
from gc_tui.services.plural import files_word

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
    "#9c7a4a",
    "#b8915c",
    "#d4a574",
    "#e0b585",
    "#ecc596",
    "#f0c896",
    "#ecc596",
    "#e0b585",
    "#d4a574",
    "#b8915c",
]

# Период счётчика кадров: общий кратный темпа глифа и рампы цвета — счётчик
# остаётся ограниченным, а обе под-анимации выходят на старт синхронно.
_ANIM_PERIOD = FRAME_HOLD * len(MORPH_FRAMES) * len(GLOW_SHADES)

# «Дыхание» точки «сверяюсь…» (судья сверяет ответ перед закрытием хода): одна
# точка • статична по форме, её ЦВЕТ плавно тускнеет↔светлеет по ТЁМНО-серой
# рампе — та же техника дыхания яркостью, что у ✦/▸, но в самых глубоких серых
# (тише всего: лесенка громкости ▸ > ✦ > •). Плавно на 0.1с-тике, без морфа формы.
_PROCESSING_SHADES = [
    "#4c525b",
    "#545b65",
    "#5d646e",
    "#666e78",
    "#6f7782",
    "#7a8290",
    "#6f7782",
    "#666e78",
    "#5d646e",
    "#545b65",
]

# «Дыхание» искры «завершаю…»: ✦ статична по форме, цвет плавно тускнеет↔
# светлеет (серая рампа, ping-pong, ~1.0с) — спокойная финализация. Та же
# техника, что пульс running-глифа ▸, но в приглушённых серых.
_VALIDATING_SHADES = [
    "#5e6675",
    "#69727f",
    "#747e8b",
    "#808c99",
    "#8c98a6",
    "#9aa4b2",
    "#8c98a6",
    "#808c99",
    "#747e8b",
    "#69727f",
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
_STATE_STOPPED_TOKENS = "stopped_tokens"
_STATE_STOPPED_TOOL_CALLS = "stopped_tool_calls"

# Исходы, которые персистентны как строка-итог (done / cancelled / …) до
# следующего user-send. Единый список для poll, resume и meta.last_indicator.
TERMINAL_INDICATOR_STATES = frozenset(
    {
        _STATE_DONE,
        _STATE_CANCELLED,
        _STATE_FAILED,
        _STATE_STOPPED_STEPS,
        _STATE_STOPPED_ERRORS,
        _STATE_STOPPED_TOKENS,
        _STATE_STOPPED_TOOL_CALLS,
    }
)


def is_terminal_indicator_state(state: str) -> bool:
    return state in TERMINAL_INDICATOR_STATES


# Минимальный зазор между строкой-итогом и правым сегментом сводки правок.
_SUMMARY_GAP = 2
# Ширина, когда измерить нельзя (виджет вне DOM): та же страховка, что у
# адаптивного футера.
_FALLBACK_WIDTH = 80


def format_turn_summary(files: int, add: int, del_: int) -> str | None:
    """«изменено 3 файла · +120 −15»; ход без правок → None.

    Знаки счётчиков — те же, что в шапке DiffBlock (`+N −M`, U+2212)."""
    if files <= 0:
        return None
    return f"изменено {files} {files_word(files)} · +{add} −{del_}"


def compose_status_line(
    head: Text,
    summary: str | None,
    available_width: int,
    *,
    hover: bool = False,
) -> tuple[Text, tuple[int, int] | None]:
    """`(строка, x-диапазон сегмента)`; `None` — сегмента в строке нет.

    Сегмент не влезает по ширине — скрываем целиком: строка индикатора ровно
    в одну клетку высотой, а обрезанное «изменено 3 фай…» врало бы о числах.

    Диапазон отдаётся вместе с текстом, потому что он известен только здесь:
    сегмент кликабелен (открывает экран изменений в источнике «Ход»), а по
    цельному `Text` координаты его первой клетки не восстановить.

    `hover` — курсор стоит на сегменте: текст берёт АКЦЕНТ. Это единственный
    язык наведения, который база знает: `.diff-more:hover` и `.block-copy:hover`
    красят `$accent`, других hover-правил в проекте нет. Сегмент — такой же
    кликабельный элемент, и отвечать руке он обязан тем же цветом.
    """
    if not summary:
        return head, None
    segment = Text(summary, style=_HOVER if hover else _DIM)
    gap = available_width - head.cell_len - segment.cell_len
    if gap < _SUMMARY_GAP:
        return head, None
    out = Text()
    out.append_text(head)
    out.append(" " * gap)
    out.append_text(segment)
    return out, (head.cell_len + gap, out.cell_len)


# Исходы хода, кроме «Выполнил» (у него своя голова — время хода): глиф и слова.
_TERMINAL_ENDINGS = {
    _STATE_CANCELLED: ("■", "Отменено"),
    _STATE_FAILED: ("✗", "Ошибка"),
    _STATE_STOPPED_STEPS: ("⏹", "Остановлено: превышен лимит шагов"),
    _STATE_STOPPED_ERRORS: ("⏹", "Остановлено: превышен лимит ошибок"),
    _STATE_STOPPED_TOKENS: ("⏹", "Остановлено: превышен лимит токенов"),
    _STATE_STOPPED_TOOL_CALLS: (
        "⏹",
        "Остановлено: превышен лимит вызовов инструментов",
    ),
}


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
        turn_summary: tuple[int, int, int] | None = None,
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
        self._anim_timer = None
        super().__init__(
            self._render_text(), classes="thinking-indicator", markup=False
        )

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
            _STATE_FAILED,
            _STATE_STOPPED_STEPS,
            _STATE_STOPPED_ERRORS,
            _STATE_STOPPED_TOKENS,
            _STATE_STOPPED_TOOL_CALLS,
        ):
            return
        if self._state == state:
            return
        self._state = state
        if state == _STATE_DONE and self._frozen_elapsed is None:
            self._frozen_elapsed = int(self._clock() - self._started_at)
        if self._anim_timer is not None:
            # Итог хода от кадра не зависит — таймер без дела не тикает.
            if is_terminal_indicator_state(state):
                self._anim_timer.pause()
            else:
                self._anim_timer.resume()
        if self.is_mounted:
            self.refresh()

    def on_mount(self) -> None:
        # Перерисовка 10 раз/сек: глиф держится FRAME_HOLD тиков (≈1.2с цикл),
        # свечение «дышит» каждый тик (1.0с на ping-pong рампы).
        # Созданный сразу конечным (восстановление) стартует на паузе.
        self._anim_timer = self.set_interval(
            0.1,
            self._tick,
            pause=is_terminal_indicator_state(self._state),
        )

    def _tick(self) -> None:
        # F4: freeze UI на время app-select — не дёргаем анимацию (Screen._selecting).
        try:
            if getattr(self.screen, "_selecting", False):
                return
        except Exception:
            pass
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
        if span is None:
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

    def _render_text(self) -> Text:
        text = Text()
        # Сегмент рисует только строка-итог «Выполнил»; во всех прочих
        # состояниях его нет, и след прошлой отрисовки обязан пропасть вместе
        # с ним — иначе клик попал бы в пустоту на месте старых цифр.
        self._summary_span = None
        if self._state == _STATE_DONE:
            text.append("✓", style=_DIM)
            text.append(" ")
            # Живой done всегда замораживает elapsed в set_state → «Выполнил за N».
            # frozen_elapsed=None — восстановление старой сессии без сохранённого
            # времени → «Выполнил» без «за N» (не выдумываем 0с).
            if self._frozen_elapsed is not None:
                text.append(
                    f"Выполнил за {format_duration(self._frozen_elapsed)}",
                    style=_DIM,
                )
            else:
                text.append("Выполнил", style=_DIM)
            line, self._summary_span = compose_status_line(
                text,
                self._turn_summary,
                self._status_width(),
                hover=self._summary_hover,
            )
            return line
        ending = _TERMINAL_ENDINGS.get(self._state)
        if ending is not None:
            # Сводка правок — у ЛЮБОГО исхода, не только у «Выполнил»: упавший
            # или остановленный ход файлы уже менял, и после ошибки знать это
            # пользователю важнее всего (ревью PR, находка 7).
            glyph, label = ending
            text.append(glyph, style=_DIM)
            text.append(" ")
            text.append(label, style=_DIM)
            line, self._summary_span = compose_status_line(
                text,
                self._turn_summary,
                self._status_width(),
                hover=self._summary_hover,
            )
            return line
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
            units = (
                self._sink.progress_units() if self._sink is not None else 0
            )
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
                            parens += " · обдумал"  # <0.5с → без «за 0с»
                        else:
                            parens += (
                                f" · обдумал за {format_duration(round(secs))}"
                            )
        parens += ")"
        text.append(parens)
        return text
