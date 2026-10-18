"""ToolCallRow — единый виджет на один tool-вызов; мутирует waiting → running → ok/err.

Layout: Horizontal(prefix-Static, content-Static) — wrap длинных args/result
происходит внутри content-колонки = hanging indent (continuation выравнивается
под началом имени тула, col 5 относительно левого края строки).

Статус-глиф живёт в prefix-Static (▸/✓/✗/·), content-Static несёт имя/args/хвост.

Фазы:
- waiting  — call уже нарисован, execute ещё не начался: · dim, без elapsed;
- running  — сейчас исполняется: ▸ + elapsed (+ опциональный live_status);
- done     — есть result: ✓/✗;
- interrupted — стоп/обрыв до output: · + «прервано».
"""

from __future__ import annotations

import time
from typing import Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from gc_tui.palette import DIM as _DIM
from gc_tui.palette import ERR as _ERR
from gc_tui.palette import OK as _OK
from gc_tui.palette import RUN_COLOR as _RUN
from gc_tui.palette import TOOL_NAME as _TOOL_NAME
from gc_tui.services.duration_format import format_duration
from gc_tui.widgets.click_model import ClickModelMixin
from gc_tui.widgets.static_content import update_if_changed

# Пульс яркости running-глифа ▸: амбер тускло→ярко→тускло (ping-pong, пик —
# RUN_COLOR). Двигается на собственном 0.1с-тике строки (см. on_mount) → плавное
# дыхание 10fps; 10 кадров → цикл ≈1.0с.
_PULSE_SHADES = [
    "#8a6a3a",
    "#977448",
    "#a47f55",
    "#b18a62",
    "#c4986b",
    "#d4a574",
    "#c4986b",
    "#b18a62",
    "#a47f55",
    "#977448",
]


class ToolCallRow(ClickModelMixin, Widget):
    """Строка одного tool-вызова: waiting / running / ok / error / interrupted.

    DOM:
        ToolCallRow
            Horizontal
                Static(<rich Text>, classes="trace-prefix")  — ведущий статус-глиф ▸/✓/✗/·
                Static(<rich Text>, classes="trace-content line-tool")  — имя/args/хвост

    Цветовые зоны через rich.Text:
    - prefix: статус-глиф — RUN (пульс) / OK / ERR / DIM.
    - content: name → TOOL_NAME, скобки/args/хвост → DIM, elapsed → RUN.

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
    """
    can_focus = True

    _PREFIX = "   "  # отступ-индент тула (без «лесенки» |-); статус-глиф ✓/✗/▸ — следом
    _NESTED_PREFIX = "       "  # nested под subagent: +4 пробела

    def __init__(
        self,
        *,
        call_id: str,
        tool_name: str,
        args_summary: str,
        event_no: int = 0,
        waiting: bool = False,
        nested: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.call_id = call_id
        self._tool_name = tool_name
        self._args = args_summary
        self._event_no = event_no
        self._result: Optional[str] = None
        self._is_error: bool = False
        self._interrupted: bool = (
            False  # терминальное «прервано» (стоп/обрыв run)
        )
        self._waiting: bool = bool(waiting)
        self._nested: bool = bool(nested)
        self._live_status: Optional[str] = (
            None  # step/last для корневого субагента
        )
        self._content_static: Optional[Static] = None
        self._prefix_static: Optional[Static] = None
        self._pulse_frame: int = 0
        self._pulse_timer = (
            None  # собственный 0.1с-тик дыхания ▸ (_sync_pulse)
        )
        # line-tool на самом widget'е — color: $dim каскадится в prefix-Static.
        self.add_class("line-tool")
        if self._nested:
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

    def on_mount(self) -> None:
        self._sync_pulse()

    @property
    def _is_live(self) -> bool:
        """Running-строка: не waiting, без результата, не прервана."""
        return not (
            self._waiting or self._result is not None or self._interrupted
        )

    def _sync_pulse(self) -> None:
        """Таймер дыхания ▸ есть только у запущенной running-строки.

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
        """Кадр дыхания ▸. Waiting / done / interrupted — не тикаем."""
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
            # Меняется только цвет ▸ (ширина глифа постоянна) — без layout.
            self._prefix_static.update(self._render_prefix(), layout=False)

    def _update(self) -> None:
        if self._prefix_static is not None:
            update_if_changed(self._prefix_static, self._render_prefix())
        if self._content_static is not None:
            update_if_changed(self._content_static, self._render_content())

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
        """Снять waiting → running (▸ + elapsed). После result/interrupt — no-op."""
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

    def set_result(self, *, summary: str, error: bool) -> None:
        self._result = summary
        self._is_error = error
        self._waiting = False
        self._live_status = None
        self._sync_pulse()
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
        self._sync_pulse()

    def resume(self) -> None:
        if self._paused_at is not None:
            self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None
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
            if self._interrupted or self._waiting:
                return "·", _DIM
            return "▸", _PULSE_SHADES[self._pulse_frame % len(_PULSE_SHADES)]
        if self._is_error:
            return "✗", _ERR
        return "✓", _OK

    def _indent_prefix(self) -> str:
        return self._NESTED_PREFIX if self._nested else self._PREFIX

    def _render_prefix(self) -> Text:
        text = Text()
        text.append(self._indent_prefix(), style=_DIM)
        glyph, color = self._status_glyph()
        text.append(glyph, style=color)
        text.append(" ")
        return text

    def _render_content(self) -> Text:
        text = Text()
        text.append(self._tool_name, style=_TOOL_NAME)
        text.append("(", style=_DIM)
        text.append(self._args, style=_DIM)
        if self._result is None:
            text.append(") ", style=_DIM)
            if self._interrupted:
                text.append("прервано", style=_DIM)
            elif self._waiting:
                pass  # только имя(args), без elapsed
            elif self._live_status:
                text.append(f"· {self._live_status} · ", style=_DIM)
                text.append(format_duration(self._elapsed_s()), style=_RUN)
            else:
                text.append(
                    f"· {format_duration(self._elapsed_s())}", style=_RUN
                )
            return text
        text.append(")", style=_DIM)
        if self._is_error or self._result:
            text.append(f" -> {self._result}", style=_DIM)
        return text

    def _open_request(self) -> Optional[Message]:
        # Клик/Enter → детальный экран call + output.
        from gc_tui.messages import OpenToolCallDetailRequest

        return OpenToolCallDetailRequest(self.call_id)
