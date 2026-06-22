"""ToolCallRow — единый виджет на один tool-вызов; мутирует от running к ok/err.

Layout: Horizontal(prefix-Static, content-Static) — wrap длинных args/result
происходит внутри content-колонки = hanging indent (continuation выравнивается
под началом имени тула, col 5 относительно левого края строки)."""
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


class ToolCallRow(Widget):
    """Строка одного tool-вызова с состоянием pending / ok / error.

    DOM:
        ToolCallRow
            Horizontal
                Static("  |- ", classes="trace-prefix")
                Static(<rich Text>, classes="trace-content line-tool")

    Цветовые зоны через rich.Text в content:
    - name → TOOL_NAME (primary), скобки/args/path/result → DIM (обвес).
    - [ok] → OK / [!] err → ERR / [..] running → RUN_COLOR.

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
        # line-tool на самом widget'е — color: $dim каскадится в prefix-Static,
        # чтобы `  |- ` отрисовалось dim (как и было до compose-рефакторинга).
        self.add_class("line-tool")
        # Момент создания строки — для тикающего elapsed в running-состоянии.
        self._start_monotonic = time.monotonic()

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static(self._PREFIX, classes="trace-prefix", markup=False)
            self._content_static = Static(
                self._render_content(),
                classes="trace-content line-tool",
                markup=False,
            )
            yield self._content_static

    def set_result(self, *, summary: str, error: bool) -> None:
        """Завершить вызов; обновить content-Static."""
        self._result = summary
        self._is_error = error
        if self._content_static is not None:
            self._content_static.update(self._render_content())

    def mark_interrupted(self) -> None:
        """Терминальное состояние «прервано»: run остановлен/оборвался до output'а.

        Не ok и не err — dim, без алармизма. После реального результата — no-op
        (поздний output ценнее факта остановки); повторный вызов — no-op."""
        if self._result is not None or self._interrupted:
            return
        self._interrupted = True
        if self._content_static is not None:
            self._content_static.update(self._render_content())

    def refresh_running_elapsed(self) -> None:
        """Перерисовать content, пока вызов в running (тикающий elapsed).
        После завершения/прерывания — no-op (рендер уже терминальный)."""
        if self._result is None and not self._interrupted \
                and self._content_static is not None:
            self._content_static.update(self._render_content())

    def _elapsed_s(self) -> int:
        """Целые секунды с момента создания строки (для running-рендера)."""
        return int(time.monotonic() - self._start_monotonic)

    def _render_content(self) -> Text:
        """Вернуть rich.Text для content-колонки (без prefix '  |- ')."""
        text = Text()
        if self._result is None:
            if self._interrupted:
                # `[x] name(args) прервано` — весь ряд dim, без цвета-сигнала.
                text.append("[x] ", style=_DIM)
                text.append(self._tool_name, style=_TOOL_NAME)
                text.append("(", style=_DIM)
                text.append(self._args, style=_DIM)
                text.append(") ", style=_DIM)
                text.append("прервано", style=_DIM)
                return text
            # pending: `[..] name(args) running · Nс`
            text.append("[..] ", style=_RUN)
            text.append(self._tool_name, style=_TOOL_NAME)
            text.append("(", style=_DIM)
            text.append(self._args, style=_DIM)
            text.append(") ", style=_DIM)
            text.append(f"running · {format_duration(self._elapsed_s())}", style=_RUN)
            return text

        if self._is_error:
            text.append(self._tool_name, style=_TOOL_NAME)
            text.append("(", style=_DIM)
            text.append(self._args, style=_DIM)
            text.append(") ", style=_DIM)
            text.append("[!] ", style=_ERR)
            text.append(self._result, style=_ERR)
            return text

        # ok: `name(args)` + optional ` -> result` + ` [ok]`
        text.append(self._tool_name, style=_TOOL_NAME)
        text.append("(", style=_DIM)
        text.append(self._args, style=_DIM)
        text.append(")", style=_DIM)
        if self._result:
            text.append(f" -> {self._result}", style=_DIM)
        text.append(" ")
        text.append("[ok]", style=_OK)
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
