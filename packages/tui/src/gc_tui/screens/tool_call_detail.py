"""ToolCallDetailScreen — детальный просмотр одного tool-вызова:
CALL payload (name + полные args) и OUTPUT payload (полный, не сокращённый).

spec 2026-05-27 / tool-output-detail.
"""

from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from gc_tui.models import TraceEvent
from gc_tui.services.json_payload import render_payload


def _section_header(label: str) -> Horizontal:
    """Длинная линия `─` с вырезом-лейбом по центру. spec 2026-05-27.
    Реализация: Horizontal из 3 Static — левая rule (1fr) + label (auto) + правая rule (1fr)."""
    return Horizontal(
        Static("─" * 200, classes="detail-section-rule", markup=False),
        Static(label, classes="detail-section-label", markup=False),
        Static("─" * 200, classes="detail-section-rule", markup=False),
        classes="detail-section-header",
    )


class ToolCallDetailScreen(Screen):
    """Детальный экран: header + scroll(CALL section + OUTPUT section) + footer."""

    def __init__(
        self, *, call_event: TraceEvent, result_event: Optional[TraceEvent]
    ) -> None:
        super().__init__()
        self._call_event = call_event
        self._result_event = result_event

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            yield Static(self._header_text(), id="header", markup=False)
            with VerticalScroll(id="trace"):
                yield _section_header("CALL")
                yield render_payload(self._call_event.full_payload)
                yield _section_header("OUTPUT")
                yield self._output_widget()
            yield Static(
                "[Esc] back  [↑↓ / PgUp PgDn] прокрутка",
                id="footer",
                markup=False,
            )

    def _header_text(self) -> str:
        ev = self._call_event
        tool = ev.tool_name or "<tool>"
        # short_args: то что mapper вычислил для row (summary без 'name(')
        short_args = ev.summary or ""
        if "(" in short_args:
            short_args = short_args.split("(", 1)[1].rstrip(")")
        return f"Viewer: {tool}({short_args}) · #{ev.no}"

    def _output_widget(self):
        if self._result_event is None:
            return Static(
                "(результат ещё не получен)", classes="line-meta", markup=False
            )
        # full_payload result-event: {type: function_call_output, output: <str>}.
        # Нас интересует именно поле output.
        payload = self._result_event.full_payload
        output_val = (
            payload.get("output") if isinstance(payload, dict) else payload
        )
        if output_val is None:
            return Static("(пустой output)", classes="line-meta", markup=False)
        return render_payload(output_val)
