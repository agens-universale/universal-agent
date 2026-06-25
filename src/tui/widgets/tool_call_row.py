"""ToolCallRow — единый виджет на один tool-вызов; мутирует от running к ok/err.

Layout: Horizontal(prefix-Static, content-Static) — wrap длинных args/result
происходит внутри content-колонки = hanging indent (continuation выравнивается
под началом имени тула, col 5 относительно левого края строки).

Статус-глиф живёт в prefix-Static (▸/✓/✗/·), content-Static несёт имя/args/хвост.
"""
from __future__ import annotations

import time
from typing import Optional

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static

from tui.palette import DIM as _DIM
from tui.palette import ERR as _ERR
from tui.palette import OK as _OK
from tui.palette import RUN_COLOR as _RUN
from tui.palette import TOOL_NAME as _TOOL_NAME
from tui.services.duration_format import format_duration

# Пульс яркости running-глифа ▸: амбер тускло→ярко→тускло (ping-pong зашит).
# Кадр сменяется на каждом тике обновления running-строк (_poll_active ~250мс).
_PULSE_SHADES = ["#8a6a3a", "#a8814f", "#c69a64", "#d4a574", "#c69a64", "#a8814f"]


class ToolCallRow(Widget):
    """Строка одного tool-вызова с состоянием pending / ok / error.

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

    _PREFIX = "  |- "

    def __init__(self, *, call_id: str, tool_name: str,
                 args_summary: str, event_no: int = 0, **kwargs) -> None:
        super().__init__(**kwargs)
        self.call_id = call_id
        self._tool_name = tool_name
        self._args = args_summary
        self._event_no = event_no
        self._result: Optional[str] = None
        self._is_error: bool = False
        self._interrupted: bool = False  # терминальное «прервано» (стоп/обрыв run)
        self._content_static: Optional[Static] = None
        self._prefix_static: Optional[Static] = None
        self._pulse_frame: int = 0
        # line-tool на самом widget'е — color: $dim каскадится в prefix-Static,
        # чтобы `  |- ` отрисовалось dim (как и было до compose-рефакторинга).
        self.add_class("line-tool")
        # Момент создания строки — для тикающего elapsed в running-состоянии.
        self._start_monotonic = time.monotonic()
        # Пауза тикера на время человеко-ожидания (confirm / ask_human): агент
        # заблокирован на решении юзера — это не работа тула, простой исключаем.
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

    def _update(self) -> None:
        """Перерисовать prefix (статус-глиф) и content (имя/хвост)."""
        if self._prefix_static is not None:
            self._prefix_static.update(self._render_prefix())
        if self._content_static is not None:
            self._content_static.update(self._render_content())

    def set_result(self, *, summary: str, error: bool) -> None:
        """Завершить вызов; обновить prefix+content."""
        self._result = summary
        self._is_error = error
        self._update()

    def mark_interrupted(self) -> None:
        """Терминальное «прервано»: run остановлен/оборвался до output'а.
        После реального результата — no-op; повторный вызов — no-op."""
        if self._result is not None or self._interrupted:
            return
        self._interrupted = True
        self._update()

    def refresh_running_elapsed(self) -> None:
        """Тик running-строки: продвинуть пульс + перерисовать. После
        завершения/прерывания — no-op."""
        if self._result is not None or self._interrupted:
            return
        self._pulse_frame += 1
        self._update()

    def pause(self) -> None:
        """Заморозить тикер elapsed (человеко-ожидание confirm/ask_human).
        Идемпотентно; после завершения/прерывания — no-op."""
        if self._paused_at is None and self._result is None and not self._interrupted:
            self._paused_at = time.monotonic()

    def resume(self) -> None:
        """Снять паузу: накопить простой в _paused_total. Без паузы — no-op."""
        if self._paused_at is not None:
            self._paused_total += time.monotonic() - self._paused_at
            self._paused_at = None

    def _elapsed_s(self) -> int:
        """Целые секунды работы тула (за вычетом простоя на человеко-ожидание).
        Во время паузы — заморожено на момент паузы."""
        now = self._paused_at if self._paused_at is not None else time.monotonic()
        return int(now - self._start_monotonic - self._paused_total)

    def _status_glyph(self) -> tuple[str, str]:
        """(глиф, цвет) статуса для prefix-зоны."""
        if self._result is None:
            if self._interrupted:
                return "·", _DIM
            return "▸", _PULSE_SHADES[self._pulse_frame % len(_PULSE_SHADES)]
        if self._is_error:
            return "✗", _ERR
        return "✓", _OK

    def _render_prefix(self) -> Text:
        """`  |- <статус-глиф> ` — лесенка (dim) + цветной ведущий глиф."""
        text = Text()
        text.append(self._PREFIX, style=_DIM)   # "  |- "
        glyph, color = self._status_glyph()
        text.append(glyph, style=color)
        text.append(" ")
        return text

    def _render_content(self) -> Text:
        """rich.Text для content-колонки (имя/args/хвост, без статус-глифа)."""
        text = Text()
        text.append(self._tool_name, style=_TOOL_NAME)
        text.append("(", style=_DIM)
        text.append(self._args, style=_DIM)
        if self._result is None:
            text.append(") ", style=_DIM)
            if self._interrupted:
                text.append("прервано", style=_DIM)
            else:
                text.append(f"· {format_duration(self._elapsed_s())}", style=_RUN)
            return text
        text.append(")", style=_DIM)
        # ok и error: `-> хвост` серым (цвет несёт ведущий глиф в prefix).
        # error — всегда показываем хвост (текст ошибки); ok — только если непустой.
        if self._is_error or self._result:
            text.append(f" -> {self._result}", style=_DIM)
        return text

    def _on_focus(self, event) -> None:  # noqa: ANN001
        # super() ПЕРВЫМ: Widget._on_focus ставит self.has_focus = True,
        # иначе _render_content() ниже прочитает stale Reactive=False.
        super()._on_focus(event)
        from tui.messages import ToolCallFocusChanged
        self.post_message(ToolCallFocusChanged(self.call_id, focused=True))

    def _on_blur(self, event) -> None:  # noqa: ANN001
        # super() ПЕРВЫМ: см. _on_focus.
        super()._on_blur(event)
        from tui.messages import ToolCallFocusChanged
        self.post_message(ToolCallFocusChanged(self.call_id, focused=False))

    def on_click(self, event: events.Click) -> None:
        # Двойной клик → открыть детальный экран call + output.
        if event.chain == 2:
            from tui.messages import OpenToolCallDetailRequest
            self.post_message(OpenToolCallDetailRequest(self.call_id))

    def on_key(self, event: events.Key) -> None:
        # Enter на сфокусированной строке → детальный экран call + output.
        if event.key == "enter":
            event.stop()
            from tui.messages import OpenToolCallDetailRequest
            self.post_message(OpenToolCallDetailRequest(self.call_id))
