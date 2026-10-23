"""ToolCallRow — единый виджет на один tool-вызов; мутирует waiting → running → ok/err.

Layout: Horizontal(prefix-Static, content-Static) — wrap длинного объекта/
результата происходит внутри content-колонки = hanging indent (continuation
выравнивается под началом подписи: отступ уровня + глиф + пробел). Под ней —
слот следствий `└` (ConsequenceSlot.trace-consequence) на +2 от глифа: текст
ошибки и пометки вызова (спека P3 §3); пустой — скрыт.

Статус-глиф живёт в prefix-Static (●/○/•/‖), content-Static несёт
`Подпись  объект` и хвост ` · сводка` успеха; у ошибки хвоста нет.
Первая строка всегда в одну строку: подпись и ❐ не режутся, объект
сокращается `…` под ширину content-колонки (`fit_call_target`; пересчёт —
при смене ширины или содержимого, не на тике дыхания). Длинный хвост
(сводка, живой статус) режется `…`, когда объект дошёл до своего минимума
(`CALL_TARGET_MIN_COLS`); таймер и счётчики правки не режутся.
У строки правки (`set_edit_counts`) после объекта — счётчики `(+a −r)`, в конце
строки справа — кнопка ❐ копирования её диффа (спека P3 §10).

Фазы:
- waiting  — call уже нарисован, execute ещё не начался: • dim, без elapsed;
- running  — сейчас исполняется: ● (дыхание амбером) + elapsed
  (+ опциональный live_status);
  на человеко-ожидании (pause) — ‖ без дыхания, elapsed заморожен;
- done     — есть result: ● зелёным / красным; стоп или лимит (`halted`) —
  ○ серым, отказ (`refused`) — ○ и строка `└` цветом REFUSED;
- interrupted — стоп/обрыв до output: ○ серым + «прервано».

Режимы блока P4 (`set_mode`, решения модели `gc_tui.services.tool_blocks`):
- single — одиночная строка, как выше;
- header — заголовок блока: глиф вида блока и жирное слово `INK`, хвост из
  решения модели, без таймера; Enter — экран последнего члена
  (`detail_call_id`);
- member — член блока: без глифа, текст в колонке 4 (`└ ` в колонке 2 у
  первого видимого), глагол `DIM`, объект и хвост `MUTED`, идущий — `INK`;
  `└` следствия — на шаг глубже текста.
`Ran` (`set_command`, `set_output`, `set_parts_expanded`): продолжение
команды — строками под первой (колонка 4), вывод — первым следствием `└`;
раскрытие — вся команда и весь вывод.
Застывший таймер (`set_elapsed`; имя `set_timer` занято
таймерами Textual) появляется только по решению модели: строка
без модели выглядит как прежде. Вызов быстрее секунды — без таймера.
"""

from __future__ import annotations

import time
from typing import Callable, Literal, Optional

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from gc_tui import textual_compat
from gc_tui.models import ResultOutcome
from gc_tui.palette import DIM as _DIM
from gc_tui.palette import ERR as _ERR
from gc_tui.palette import INK as _INK
from gc_tui.palette import MUTED as _MUTED
from gc_tui.palette import OK as _OK
from gc_tui.palette import REFUSED as _REFUSED
from gc_tui.palette import RUN_COLOR as _RUN
from gc_tui.palette import SKILL as _SKILL
from gc_tui.palette import SUB_INK as _SUB_INK
from gc_tui.services.duration_format import format_duration
from gc_tui.services.tool_blocks import (
    COMMAND_PREVIEW_LINES,
    Parts,
    WordRole,
    counts_text,
)
from gc_tui.services.trace_glyphs import (
    CALL_GLYPH,
    CALL_UNDONE_GLYPH,
    CALL_WAITING_GLYPH,
    blink_on,
)
from gc_tui.services.trace_render import (
    AGENT_TASK_LINES,
    CALL_TARGET_MIN_COLS,
    fit_call_target,
    fit_lines,
    level_prefix,
)
from gc_tui.widgets.block_panel import (
    CopyFeedbackMixin,
    _copy_request,
    copy_glyph,
)
from gc_tui.widgets.click_model import ClickModelMixin
from gc_tui.widgets.consequence_line import ConsequenceSlot
from gc_tui.widgets.no_select import NoSelectStatic
from gc_tui.widgets.static_content import update_if_changed

# Running-строка на человеко-ожидании (confirm / ask_human): статичный глиф
# паузы в базовом амбере, без дыхания; elapsed заморожен. Не ⏸: тот числится
# эмодзи, и часть терминалов (Windows Terminal) рисует его в две клетки.
_PAUSE_GLYPH = "‖"
# Пустая клетка справа у полной строки: хвост не прижат к полосе прокрутки.
_RIGHT_GUTTER = 1
#: Застывший таймер — с этой длительности; быстрее — без таймера.
_FROZEN_TIMER_MIN_S = 1.0
# Между подписью и объектом; слово с тем же разделителем (`Running` /
# `Ran`) — подпись команды.
_CAPTION_SEP = "  "

#: Итог вызова: глиф, его цвет и цвет строки `└` итога. Стоп и лимит —
#: не сбой (K4), отказ — не сбой, а решение политики или человека: цветом
#: только глиф, строка серая.
_OUTCOME_LOOK: dict[str, tuple[str, str, str]] = {
    "ok": (CALL_GLYPH, _OK, _OK),
    "error": (CALL_GLYPH, _ERR, _ERR),
    "halted": (CALL_UNDONE_GLYPH, _DIM, _DIM),
    "refused": (CALL_UNDONE_GLYPH, _REFUSED, _REFUSED),
}

#: Вид цвета модели блоков (`tool_blocks.Look`) → токен палитры.
LOOK_STYLE: dict[str, str] = {
    "run": _RUN,
    "ok": _OK,
    "error": _ERR,
    "refused": _REFUSED,
    "halted": _DIM,
    "dim": _DIM,
    "ink": _INK,
    "muted": _MUTED,
}


def parts_text(parts: Parts) -> Text:
    """Части текста модели блоков — `Text` цветами палитры."""
    text = Text()
    for chunk, look in parts:
        text.append(chunk, style=LOOK_STYLE[look])
    return text


#: Глиф заголовка блока по виду (`tool_blocks.GlyphLook`); `run` мигает.
_HEADER_GLYPH: dict[str, tuple[str, str]] = {
    "ok": (CALL_GLYPH, _OK),
    "error": (CALL_GLYPH, _ERR),
    "halted": (CALL_UNDONE_GLYPH, _DIM),
}

RowMode = Literal["single", "header", "member"]


def _fit_note(note: str, room: Optional[int], used: int) -> str:
    """Свободный текст хвоста ``note`` в ``room`` клеток за вычетом уже
    занятых ``used``: с `…` с конца; места нет — пусто."""
    if room is None or used + len(note) <= room:
        return note
    cols = room - used
    if cols < len(" · x…"):
        return ""
    return note[: cols - 1].rstrip() + "…"


class _CopyButton(NoSelectStatic):
    """Кнопка ❐ строки: служебный элемент — клик по ней строку не выделяет.

    `FOCUS_ON_CLICK = False` предъявляет экран ленты
    (`click_model.focus_target_at`), иначе фокус на MouseDown взяла бы
    сама строка."""

    FOCUS_ON_CLICK = False


class ToolCallRow(CopyFeedbackMixin, ClickModelMixin, Widget):
    """Строка одного tool-вызова: waiting / running / ok / error / interrupted.

    DOM:
        ToolCallRow
            Horizontal
                Static(classes="trace-prefix")  — статус-глиф ●/○/•
                Static(classes="trace-content line-tool")  — подпись,
                    объект, хвост
                _CopyButton(classes="block-copy trace-action")  — ❐, видна
                    у строки правки
            ConsequenceSlot(classes="trace-consequence")  — строки `└`

    Цветовые зоны через rich.Text:
    - prefix: статус-глиф — RUN (пульс) / OK / ERR / DIM.
    - content: одиночная строка — слово `INK` жирным, объект `INK` /
      `SUB_INK` после конца (§10); хвост → DIM, elapsed → RUN.

    Контракт focus-handler: если в `_on_focus`/`_on_blur` synchronously читаешь
    `self.has_focus` — вызови `super()` ПЕРВЫМ, иначе прочитаешь stale Reactive-
    значение (Widget._on_focus ставит has_focus=True, MRO dispatcher вызывает
    subclass-handler раньше parent-handler).
    """

    DEFAULT_CSS = """
    ToolCallRow {
        height: auto;
        width: 1fr;
        padding: 0;
    }
    ToolCallRow > Horizontal {
        height: auto;
        width: 1fr;
    }
    ToolCallRow .block-copy {
        display: none;
        width: auto;
        height: 1;
        padding: 0 1 0 0;
    }
    ToolCallRow.edited .block-copy { display: block; }
    ToolCallRow .trace-consequence {
        display: none;
        width: 1fr;
        height: auto;
    }
    ToolCallRow.has-consequence .trace-consequence { display: block; }
    """
    can_focus = True

    def __init__(
        self,
        *,
        call_id: str,
        label: str,
        target: str,
        wire_name: str = "",
        event_no: int = 0,
        waiting: bool = False,
        level: int = 0,
        path_target: bool = False,
        skill_target: bool = False,
        task: str = "",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.call_id = call_id
        # Подпись и объект — из ядра (trace_render.build_call_row); wire-имя
        # в строку не попадает, хранится для отладки и тестов.
        self._label = label
        self._target = target
        # Объект — путь: сокращается серединой, иначе с конца.
        self._path_target = path_target
        # Объект — имя скилла: цветом скилла, как в палитре и реплике.
        self._skill_target = skill_target
        # Задача субагента — первой строкой `└` под `Agent` (§7).
        self._agent_task = task
        # Последнее сокращение объекта: (ширина, текст) — без пересчёта на
        # тиках таймера, пока ширина под объект та же.
        self._fitted: tuple[int, str] = (-1, target)
        self.wire_name = wire_name
        self._event_no = event_no
        self._result: Optional[str] = None
        # Итог вызова: глиф и цвет строки `└` (`_OUTCOME_LOOK`).
        self._outcome: ResultOutcome = "ok"
        self._interrupted: bool = (
            False  # терминальное «прервано» (стоп/обрыв run)
        )
        self._waiting: bool = bool(waiting)
        # Уровень вложенности: 0 — вызов агента, 1 — вызов его субагента.
        self._level: int = level
        self._live_status: Optional[str] = (
            None  # step/last для корневого субагента
        )
        self._content_static: Optional[Static] = None
        self._prefix_static: Optional[Static] = None
        self._consequence_slot = ConsequenceSlot(
            level, classes="trace-consequence"
        )
        # Следствия вызова сверх ошибки (пометки) — тела строк `└`.
        self._consequences: list[Text] = []
        # Правка вызова: (+N, −M) и что копирует ❐; None — не правка.
        self._edit_counts: Optional[tuple[int, int]] = None
        self._copy_text: str = ""
        self._restore_timer = None  # откат ✓ → ❐ (CopyFeedbackMixin)
        self._pulse_frame: int = 0
        self._pulse_timer = (
            None  # собственный 0.1с-тик мигания ● (_sync_pulse)
        )
        # line-tool на самом widget'е — color: $dim каскадится в prefix-Static.
        self.add_class("line-tool")
        if self._level > 0:
            self.add_class("nested-tool")
        # Момент создания / активации — для тикающего elapsed в running.
        self._start_monotonic = time.monotonic()
        # Пауза тикера на время человеко-ожидания (confirm / ask_human).
        self._paused_at: Optional[float] = None
        self._paused_total: float = 0.0
        # Конец исполнения — для застывших часов строки (`set_elapsed`).
        self._ended_at: Optional[float] = None
        # Режим блока P4 и то, что задаёт модель блоков.
        self._mode: RowMode = "single"
        self._lead = False
        self._current = False
        self._word: Optional[str] = None
        self._header: Optional[tuple[str, str, Text]] = None
        self._frozen: Optional[float] = None
        self._block_consequence: Optional[Text] = None
        self._edit_tail: Optional[Text] = None
        self._word_role: WordRole = "edit"
        # `Ran`: продолжение команды (строки после первой) и вывод
        # (свёрнутый, раскрытый) — следствием под строкой.
        self._command_rest: tuple[str, ...] = ()
        self._output: Optional[tuple[Text, Text]] = None
        self._parts_expanded = False
        self._detail: Optional[Callable[[], Optional[str]]] = None
        #: `(уровень, ключ)` строки в модели блоков — для пробела.
        self.block_key: Optional[tuple[str, str]] = None

    def compose(self) -> ComposeResult:
        with Horizontal():
            self._prefix_static = Static(
                self._render_prefix(), classes="trace-prefix", markup=False
            )
            yield self._prefix_static
            self._content_static = Static(
                self._render_content(),
                classes="trace-content line-tool",
                markup=False,
            )
            yield self._content_static
            yield _CopyButton(
                copy_glyph(), classes="block-copy trace-action", markup=False
            )
        yield self._consequence_slot

    def on_mount(self) -> None:
        self._sync_pulse()

    def on_resize(self, _event: events.Resize) -> None:
        """Новая ширина — объект первой строки и задача субагента
        сокращаются заново."""
        self._update()

    @property
    def _is_live(self) -> bool:
        """Running-строка: не waiting, без результата, не прервана."""
        return not (
            self._waiting or self._result is not None or self._interrupted
        )

    @property
    def _blinking(self) -> bool:
        """Мигает ли глиф: у члена глифа нет, у заголовка — вид `run`."""
        if self._mode == "header":
            return self._header is not None and self._header[1] == "run"
        if self._mode == "member":
            return False
        return self._is_live and self._paused_at is None

    # --- режимы блока ------------------------------------------------------

    @property
    def mode(self) -> RowMode:
        return self._mode

    @property
    def waiting(self) -> bool:
        """Ждёт своей очереди в батче (`•`): исполнение не началось."""
        return self._waiting

    @property
    def event_no(self) -> int:
        return self._event_no

    @property
    def detail_call_id(self) -> str:
        """Чей экран деталей открывает Enter: у заголовка — последнего
        члена блока."""
        detail = self._detail() if self._detail is not None else None
        return detail or self.call_id

    def set_detail_source(self, source: Callable[[], Optional[str]]) -> None:
        self._detail = source

    def set_mode(
        self,
        mode: RowMode,
        *,
        lead: bool,
        current: bool = False,
        gap: bool = False,
    ) -> None:
        """Режим строки в блоке: одиночная, заголовок или член."""
        self._mode = mode
        self._lead = lead
        self._current = current
        self.set_class(mode == "member", "-member")
        self.set_class(gap, "-gap")
        depth = 2 if mode == "member" else 0
        self._consequence_slot.set_level(self._level + depth)
        self._sync_pulse()
        self._update()

    def set_header(self, word: str, *, glyph: str, tail: Text) -> None:
        """Заголовок блока: слово, вид глифа (`run` / `ok` / `error` /
        `halted`) и хвост."""
        self._header = (word, glyph, tail)
        self._sync_pulse()
        self._update()

    def set_word(self, word: str, *, role: WordRole = "edit") -> None:
        """Слово вместо подписи категории; ``role`` задаёт его вид: глагол
        правки — через пробел, слово команды и `Agent` — через два, по
        §10. Пусто — без слова."""
        self._word = word
        self._word_role = role
        self._update()

    def set_command(self, lines: tuple[str, ...]) -> None:
        """Команда `Ran` построчно: первая — объект строки, остальные —
        продолжение под ней (колонка 4)."""
        if not lines:
            return
        self._target = lines[0]
        self._fitted = (-1, lines[0])
        self._command_rest = tuple(lines[1:])
        self._update()

    def set_output(self, collapsed: Text, expanded: Text) -> None:
        """Вывод `Ran` — первое следствие под строкой: превью и весь."""
        self._output = (collapsed, expanded)
        self._update()

    def set_parts_expanded(self, expanded: bool) -> None:
        """Блок `Ran` раскрыт: вся команда и весь вывод."""
        self._parts_expanded = expanded
        self._update()

    def set_elapsed(self, seconds: Optional[float]) -> None:
        """Застывший таймер: ``seconds`` или, если `None`, часы строки на
        конце исполнения."""
        if seconds is not None:
            self._frozen = seconds
        else:
            self._frozen = float(self._elapsed_s(until=self._ended_at))
        self._update()

    def set_consequence(self, body: Text) -> None:
        """Строка `└` исхода от модели — вместо строки по результату."""
        self._block_consequence = body
        self._update()

    def set_edit_tail(self, tail: Text) -> None:
        """Хвост файла после объекта (счётчики, размер) — вместо `(+a −r)`."""
        self._edit_tail = tail
        self._update()

    def _sync_pulse(self) -> None:
        """Таймер мигания ● есть только у запущенной running-строки.

        Waiting, результат, прерывание, пауза на человеко-ожидании или
        виджет ещё не запущен (рендер истории) — без таймера. Идемпотентно.
        """
        if self._blinking and self.is_running:
            if self._pulse_timer is None:
                self._pulse_timer = self.set_interval(0.1, self._pulse_tick)
        elif self._pulse_timer is not None:
            self._pulse_timer.stop()
            self._pulse_timer = None

    def _pulse_tick(self) -> None:
        """Кадр мигания ●. Waiting / done / interrupted — не тикаем."""
        # F4: freeze UI на время app-select — не тикаем анимацию (Screen._selecting).
        try:
            if textual_compat.is_selecting(self.screen):
                return
        except Exception:
            pass
        if not self._blinking:
            return
        self._pulse_frame += 1
        if self._prefix_static is not None:
            # Меняется только цвет ● (ширина глифа постоянна) — без layout.
            self._prefix_static.update(self._render_prefix(), layout=False)

    def _update(self) -> None:
        if self._prefix_static is not None:
            update_if_changed(self._prefix_static, self._render_prefix())
        if self._content_static is not None:
            update_if_changed(self._content_static, self._render_content())
        bodies = self._consequence_bodies()
        self.set_class(bool(bodies), "has-consequence")
        self._consequence_slot.set_bodies(bodies)

    def set_active(self) -> None:
        """Снять waiting → running (● + elapsed). После result/interrupt — no-op."""
        if self._result is not None or self._interrupted:
            return
        if not self._waiting:
            return
        self._waiting = False
        self._start_monotonic = time.monotonic()
        self._paused_at = None
        self._paused_total = 0.0
        self._sync_pulse()
        self._update()

    def set_live_status(self, text: Optional[str]) -> None:
        """Хвост running-строки (step/last). None — только `· Ns`."""
        if not self._is_live:
            return
        self._live_status = text
        self._update()

    def set_result(self, *, summary: str, outcome: ResultOutcome) -> None:
        """Итог вызова: успех — хвост ` · summary`; ошибка, стоп или лимит
        и отказ — глиф исхода и строка `└ summary` его цветом."""
        self._result = summary
        self._outcome = outcome
        self._waiting = False
        self._live_status = None
        self._ended_at = self._ended_at or time.monotonic()
        self._sync_pulse()
        self._update()

    def add_consequence(self, body: Text) -> None:
        """Дописать строку `└ body` под строкой вызова (пометка вызова)."""
        self._consequences.append(body)
        self._update()

    def set_edit_counts(
        self, add: int, removed: int, *, copy_text: str
    ) -> None:
        """Строка правки: ` (+a −r)` после объекта (обе части всегда) и ❐
        в конце строки.

        `copy_text` — что кладёт в буфер ❐: весь дифф правки, как и двойной
        клик по телу блока под строкой; пусто — ❐ нет, счётчики есть."""
        self._edit_counts = (add, removed)
        self._copy_text = copy_text
        self.set_class(bool(copy_text), "edited")
        self._update()

    def mark_interrupted(self) -> None:
        """Терминальное «прервано». После результата — no-op; повтор — no-op."""
        if self._result is not None or self._interrupted:
            return
        self._interrupted = True
        self._waiting = False
        self._live_status = None
        self._ended_at = self._ended_at or time.monotonic()
        self._sync_pulse()
        self._update()

    def refresh_running_elapsed(self) -> None:
        """Тик elapsed (~0.25с). Waiting / done / interrupted — no-op."""
        if not self._is_live:
            return
        self._update()

    def pause(self) -> None:
        """Заморозить elapsed на человеко-ожидании. Waiting/done — no-op."""
        if self._paused_at is None and self._is_live:
            self._paused_at = time.monotonic()
            self._update()
        self._sync_pulse()

    def resume(self) -> None:
        if self._paused_at is not None:
            self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None
            self._update()
        self._sync_pulse()

    def _elapsed_s(self, until: Optional[float] = None) -> int:
        now = self._paused_at or until or time.monotonic()
        return int(now - self._start_monotonic - self._paused_total)

    def _status_glyph(self) -> tuple[str, str]:
        if self._mode == "header":
            look = self._header[1] if self._header is not None else "ok"
            if look == "run":
                on = blink_on(self._pulse_frame)
                return (CALL_GLYPH if on else " "), _RUN
            return _HEADER_GLYPH[look]
        if self._result is None:
            if self._interrupted:
                return CALL_UNDONE_GLYPH, _DIM
            if self._waiting:
                return CALL_WAITING_GLYPH, _DIM
            if self._paused_at is not None:
                return _PAUSE_GLYPH, _RUN
            # Мигает исчезая: в паузе — пустая клетка той же ширины.
            on = blink_on(self._pulse_frame)
            return (CALL_GLYPH if on else " "), _RUN
        glyph, style, _line_style = _OUTCOME_LOOK[self._outcome]
        return glyph, style

    def _render_prefix(self) -> Text:
        text = Text()
        text.append(level_prefix(self._level), style=_DIM)
        if self._mode == "member":
            text.append("  " + ("└ " if self._lead else "  "), style=_DIM)
            return text
        glyph, color = self._status_glyph()
        text.append(glyph, style=color)
        text.append(" ")
        return text

    def _render_content(self) -> Text:
        if self._mode == "header":
            word, _glyph, tail = self._header or ("", "", Text())
            text = Text()
            text.append(word, style=f"bold {_INK}")
            text.append_text(tail)
            return text
        label = self._label if self._word is None else self._word
        label_style, target_style = self._caption_styles()
        text = Text()
        text.append(label, style=label_style)
        sep = self._caption_sep()
        tail = self._render_tail(self._tail_room(text.cell_len, len(sep)))
        target = self._fit_target(
            text.cell_len + tail.cell_len + len(sep) + _RIGHT_GUTTER
        )
        if target:
            start = len(text.plain) + len(sep)
            text.append(f"{sep}{target}", style=target_style)
            if self._skill_target:
                text.stylize(_SKILL, start, start + len(target))
        text.append_text(tail)
        self._append_command_rest(text, target_style)
        return text

    def _append_command_rest(self, text: Text, style: str) -> None:
        """Продолжение команды под строкой: в свёрнутом виде — до двух
        строк (§4.0), раскрытом — все; длинная строка — `…` по ширине."""
        rest = self._command_rest
        if not self._parts_expanded:
            rest = rest[: COMMAND_PREVIEW_LINES - 1]
        width = self._content_width()
        for line in rest:
            if width > 2:
                line = fit_call_target(line, width - 2, path=False)
            text.append("\n  " + line, style=style)

    def _caption_sep(self) -> str:
        """Между подписью и объектом: два пробела; у слова — его
        разделитель (`Edited путь`, `Ran  команда`); без слова — ничего."""
        if self._word is None or self._word_role != "edit":
            return _CAPTION_SEP
        return " " if self._word else ""

    def _caption_styles(self) -> tuple[str, str]:
        """Цвет подписи и объекта: у члена — `DIM` / `MUTED`, у идущего —
        `INK`; путь члена-правки (без глагола) — `INK`; файл под командой
        — `SUB_INK` / `INK`; одиночная строка — по §10."""
        if self._mode != "member":
            return self._single_styles()
        if self._current:
            return _INK, _INK
        if self._word:
            # Файл под командой: `Added путь` — глагол `SUB_INK`, путь `INK`.
            return _SUB_INK, _INK
        return _DIM, (_INK if self._word == "" else _MUTED)

    def _single_styles(self) -> tuple[str, str]:
        """Одиночная строка (§10): слово `INK` жирным; объект идущей —
        `INK`, законченной — `SUB_INK` (`Ran  команда`, MCP, `Skill`);
        имя субагента и путь правки — `INK` всегда (§6.2, §7)."""
        word = f"bold {_INK}"
        if self._word and self._word_role in ("agent", "edit"):
            return word, _INK
        if self._is_live or self._waiting:
            return word, _INK
        return word, _SUB_INK

    def _content_width(self) -> int:
        return self._content_static.size.width if self._content_static else 0

    def _tail_room(self, label_cols: int, sep: int = 2) -> Optional[int]:
        """Сколько клеток хвосту, когда объект сжат до своего минимума
        (`CALL_TARGET_MIN_COLS`); колонка ещё без ширины — без предела."""
        width = self._content_width()
        if width <= 0:
            return None
        room = width - label_cols - _RIGHT_GUTTER
        if self._target:
            room -= sep + min(len(self._target), CALL_TARGET_MIN_COLS)
        return room

    def _fit_target(self, used: int) -> str:
        """Объект под остаток ширины content-колонки после подписи, двух
        пробелов и хвоста; колонка ещё без ширины — объект целиком."""
        width = self._content_width()
        if width <= 0:
            return self._target
        cols = width - used
        if self._fitted[0] != cols:
            fitted = fit_call_target(
                self._target, cols, path=self._path_target
            )
            self._fitted = (cols, fitted)
        return self._fitted[1]

    def _render_tail(self, room: Optional[int] = None) -> Text:
        """Хвост первой строки после объекта: таймер / прервано, счётчики
        правки, сводка успеха. Свободный текст хвоста (сводка, живой статус)
        режется `…` под ``room`` клеток; таймер и счётчики — нет."""
        text = Text()
        if self._result is None:
            self._append_pending_tail(text, room)
            self._append_frozen_timer(text)
            return text
        if self._edit_tail is not None:
            # Хвост файла от модели блоков заменяет и счётчики, и сводку.
            text.append_text(self._edit_tail)
            self._append_frozen_timer(text)
            return text
        if self._edit_counts is not None:
            text.append_text(parts_text(counts_text(*self._edit_counts)))
        if self._result and not self._ends_with_line:
            note = _fit_note(f" · {self._result}", room, text.cell_len)
            text.append(note, style=self._summary_style)
        self._append_frozen_timer(text)
        return text

    @property
    def _summary_style(self) -> str:
        return _MUTED if self._mode == "member" else _DIM

    def _append_frozen_timer(self, text: Text) -> None:
        """Застывший таймер законченного вызова — ` · 2с` `DIM`; вызов
        быстрее секунды — без таймера."""
        if self._frozen is None or self._is_live or self._waiting:
            return
        if self._frozen < _FROZEN_TIMER_MIN_S:
            return
        text.append(" · ", style=_DIM)
        text.append(format_duration(self._frozen), style=_DIM)

    @property
    def _ends_with_line(self) -> bool:
        """Итог строкой `└` под вызовом, не хвостом: всё, кроме успеха."""
        return self._outcome != "ok"

    def _consequence_bodies(self) -> list[Text]:
        """Тела строк `└` под вызовом: задача субагента (`DIM`, до двух
        строк), ошибка (`ERR`), стоп/лимит или отказ (`DIM`), затем
        пометки."""
        bodies = list(self._consequences)
        if self._block_consequence is not None:
            bodies.insert(0, self._block_consequence)
        elif self._ends_with_line and self._result and self._output is None:
            style = _OUTCOME_LOOK[self._outcome][2]
            bodies.insert(0, Text(self._result, style=style))
        if self._output is not None:
            collapsed, expanded = self._output
            bodies.insert(0, expanded if self._parts_expanded else collapsed)
        if self._agent_task:
            # Тело `└` — на 4 правее глифа; ширина — от самой строки: её
            # Resize приходит раньше раскладки колонок.
            cols = self.size.width - len(level_prefix(self._level)) - 4
            task = fit_lines(self._agent_task, cols, AGENT_TASK_LINES)
            bodies.insert(0, Text(task, style=_DIM))
        return bodies

    def _append_pending_tail(
        self, text: Text, room: Optional[int] = None
    ) -> None:
        """Хвост строки без результата: прервано / elapsed (+ step/last).

        Waiting — без хвоста: только подпись и объект. У члена блока
        «прервано» не пишется: обрыв видно по `○` заголовка."""
        if self._interrupted:
            if self._mode != "member":
                text.append(" · прервано", style=_DIM)
            return
        if self._waiting:
            return
        elapsed = format_duration(self._elapsed_s())
        if self._live_status:
            note = _fit_note(
                f" · {self._live_status}", room, len(" · ") + len(elapsed)
            )
            text.append(note, style=_DIM)
        text.append(" · ", style=_DIM)
        text.append(elapsed, style=_RUN)

    def on_click(self, event: events.Click) -> None:
        """Клик по ❐ копирует дифф правки и строку не выделяет.

        Остальные клики — клик-модель строки; Ctrl+клик по кнопке открывает
        событие, как с любой точки строки. `prevent_default()` на входе и
        явный super(): иначе Textual по MRO вызвал бы миксин вторым."""
        event.prevent_default()
        widget = getattr(event, "widget", None)
        if (
            not event.ctrl
            and widget is not None
            and widget.has_class("block-copy")
        ):
            event.stop()
            self.post_message(_copy_request(self._copy_text))
            self._flash_copied()
            return
        super().on_click(event)

    def _open_request(self) -> Optional[Message]:
        # Клик/Enter → детальный экран call + output.
        from gc_tui.messages import OpenToolCallDetailRequest

        return OpenToolCallDetailRequest(self.detail_call_id)
