"""ToolCallRow — единый виджет на один tool-вызов; мутирует waiting → running → ok/err.

Layout: Horizontal(prefix-Static, content-Static) — wrap длинного объекта/
результата происходит внутри content-колонки = hanging indent (continuation
выравнивается под началом подписи: отступ уровня + глиф + пробел). Под ней —
слот следствий `└` (ConsequenceSlot.trace-consequence) на +2 от глифа: текст
ошибки и пометки вызова (спека P3 §3); пустой — скрыт.

Статус-глиф живёт в prefix-Static (●/○/•/‖), content-Static несёт
`Подпись  объект` и хвост ` · сводка` успеха; у ошибки хвоста нет.
Первая строка всегда в одну строку: подпись, хвост и ❐ не режутся, объект
сокращается `…` под ширину content-колонки (`fit_call_target`; пересчёт —
при смене ширины или содержимого, не на тике дыхания).
У строки правки (`set_edit_counts`) после объекта — счётчики `+N −M`, а в конце
строки справа — кнопка ❐ копирования её диффа (спека P3 §10).

Фазы:
- waiting  — call уже нарисован, execute ещё не начался: • dim, без elapsed;
- running  — сейчас исполняется: ● (дыхание амбером) + elapsed
  (+ опциональный live_status);
  на человеко-ожидании (pause) — ‖ без дыхания, elapsed заморожен;
- done     — есть result: ● зелёным / красным; стоп или лимит (`halted`) —
  ○ серым, отказ (`refused`) — ○ цветом REFUSED, строка `└` серым;
- interrupted — стоп/обрыв до output: ○ серым + «прервано».
"""

from __future__ import annotations

import time
from typing import Optional

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from gc_tui.models import ResultOutcome
from gc_tui.palette import DIM as _DIM
from gc_tui.palette import ERR as _ERR
from gc_tui.palette import OK as _OK
from gc_tui.palette import REFUSED as _REFUSED
from gc_tui.palette import RUN_COLOR as _RUN
from gc_tui.palette import SKILL as _SKILL
from gc_tui.palette import TOOL_NAME as _TOOL_NAME
from gc_tui.services.duration_format import format_duration
from gc_tui.services.trace_glyphs import (
    CALL_GLYPH,
    CALL_UNDONE_GLYPH,
    CALL_WAITING_GLYPH,
)
from gc_tui.services.trace_render import fit_call_target, level_prefix
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
# Ритм мигания ● в кадрах 0.1с-тика: 0.7с виден, 0.3с скрыт — кружок большую
# часть времени на месте, мигание читается как «живой», а не «пропадающий».
_BLINK_ON_TICKS = 7
_BLINK_PERIOD_TICKS = 10

#: Итог вызова: глиф, его цвет и цвет строки `└` итога. Стоп и лимит —
#: не сбой (K4), отказ — не сбой, а решение политики или человека: цветом
#: только глиф, строка серая.
_OUTCOME_LOOK: dict[str, tuple[str, str, str]] = {
    "ok": (CALL_GLYPH, _OK, _OK),
    "error": (CALL_GLYPH, _ERR, _ERR),
    "halted": (CALL_UNDONE_GLYPH, _DIM, _DIM),
    "refused": (CALL_UNDONE_GLYPH, _REFUSED, _DIM),
}


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
    - content: подпись → TOOL_NAME, объект/хвост → DIM, elapsed → RUN.

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
        """Новая ширина — объект первой строки сокращается заново."""
        if self._content_static is not None:
            update_if_changed(self._content_static, self._render_content())

    @property
    def _is_live(self) -> bool:
        """Running-строка: не waiting, без результата, не прервана."""
        return not (
            self._waiting or self._result is not None or self._interrupted
        )

    def _sync_pulse(self) -> None:
        """Таймер мигания ● есть только у запущенной running-строки.

        Waiting, результат, прерывание, пауза на человеко-ожидании или
        виджет ещё не запущен (рендер истории) — без таймера. Идемпотентно.
        """
        if self._is_live and self._paused_at is None and self.is_running:
            if self._pulse_timer is None:
                self._pulse_timer = self.set_interval(0.1, self._pulse_tick)
        elif self._pulse_timer is not None:
            self._pulse_timer.stop()
            self._pulse_timer = None

    def _pulse_tick(self) -> None:
        """Кадр мигания ●. Waiting / done / interrupted — не тикаем."""
        # F4: freeze UI на время app-select — не тикаем анимацию (Screen._selecting).
        try:
            if getattr(self.screen, "_selecting", False):
                return
        except Exception:
            pass
        if not self._is_live:
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

    def set_waiting(self) -> None:
        """Перевести в waiting (·, без elapsed). После result/interrupt — no-op."""
        if self._result is not None or self._interrupted:
            return
        if self._waiting:
            return
        self._waiting = True
        self._live_status = None
        self._sync_pulse()
        self._update()

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
        self._sync_pulse()
        self._update()

    def add_consequence(self, body: Text) -> None:
        """Дописать строку `└ body` под строкой вызова (пометка вызова)."""
        self._consequences.append(body)
        self._update()

    def set_edit_counts(
        self, add: int, removed: int, *, copy_text: str
    ) -> None:
        """Строка правки: ` +N −M` после объекта и ❐ в конце строки.

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

    def _elapsed_s(self) -> int:
        now = (
            self._paused_at
            if self._paused_at is not None
            else time.monotonic()
        )
        return int(now - self._start_monotonic - self._paused_total)

    def _status_glyph(self) -> tuple[str, str]:
        if self._result is None:
            if self._interrupted:
                return CALL_UNDONE_GLYPH, _DIM
            if self._waiting:
                return CALL_WAITING_GLYPH, _DIM
            if self._paused_at is not None:
                return _PAUSE_GLYPH, _RUN
            on = self._pulse_frame % _BLINK_PERIOD_TICKS < _BLINK_ON_TICKS
            # Мигает исчезая: в паузе — пустая клетка той же ширины.
            return (CALL_GLYPH if on else " "), _RUN
        glyph, style, _line_style = _OUTCOME_LOOK[self._outcome]
        return glyph, style

    def _render_prefix(self) -> Text:
        text = Text()
        text.append(level_prefix(self._level), style=_DIM)
        glyph, color = self._status_glyph()
        text.append(glyph, style=color)
        text.append(" ")
        return text

    def _render_content(self) -> Text:
        tail = self._render_tail()
        text = Text()
        text.append(self._label, style=_TOOL_NAME)
        target = self._fit_target(text.cell_len + tail.cell_len + 2)
        if target:
            start = len(text.plain) + 2
            text.append(f"  {target}", style=_DIM)
            if self._skill_target:
                text.stylize(_SKILL, start, start + len(target))
        text.append_text(tail)
        return text

    def _fit_target(self, used: int) -> str:
        """Объект под остаток ширины content-колонки после подписи, двух
        пробелов и хвоста; колонка ещё без ширины — объект целиком."""
        width = self._content_static.size.width if self._content_static else 0
        if width <= 0:
            return self._target
        cols = width - used
        if self._fitted[0] != cols:
            fitted = fit_call_target(
                self._target, cols, path=self._path_target
            )
            self._fitted = (cols, fitted)
        return self._fitted[1]

    def _render_tail(self) -> Text:
        """Хвост первой строки после объекта: таймер / прервано, счётчики
        правки, сводка успеха."""
        text = Text()
        if self._result is None:
            self._append_pending_tail(text)
            return text
        if self._edit_counts is not None:
            add, removed = self._edit_counts
            text.append(" ")
            text.append(f"+{add}", style=_OK)
            text.append(" ")
            text.append(f"−{removed}", style=_ERR)
        if self._result and not self._ends_with_line:
            text.append(f" · {self._result}", style=_DIM)
        return text

    @property
    def _ends_with_line(self) -> bool:
        """Итог строкой `└` под вызовом, не хвостом: всё, кроме успеха."""
        return self._outcome != "ok"

    def _consequence_bodies(self) -> list[Text]:
        """Тела строк `└` под вызовом: ошибка (`ERR`), стоп/лимит или
        отказ (`DIM`), затем пометки."""
        bodies = list(self._consequences)
        if self._ends_with_line and self._result:
            style = _OUTCOME_LOOK[self._outcome][2]
            bodies.insert(0, Text(self._result, style=style))
        return bodies

    def _append_pending_tail(self, text: Text) -> None:
        """Хвост строки без результата: прервано / elapsed (+ step/last).

        Waiting — без хвоста: только подпись и объект."""
        if self._interrupted:
            text.append(" · прервано", style=_DIM)
            return
        if self._waiting:
            return
        if self._live_status:
            text.append(f" · {self._live_status}", style=_DIM)
        text.append(" · ", style=_DIM)
        text.append(format_duration(self._elapsed_s()), style=_RUN)

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

        return OpenToolCallDetailRequest(self.call_id)
