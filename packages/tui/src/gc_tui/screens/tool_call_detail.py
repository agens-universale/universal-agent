"""ToolCallDetailScreen — детальный просмотр одного tool-вызова:
CALL payload (name + полные args) и OUTPUT payload (полный, не сокращённый).

spec 2026-05-27 / tool-output-detail.
"""

from __future__ import annotations

from typing import Optional

from gc_core.agent_edits.journal import EditRecord
from gc_core.message_policy.nudges import output_for_card
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from gc_tui.models import TraceEvent
from gc_tui.services.diff_render import render_hunks
from gc_tui.services.json_payload import render_payload, unfold_payload

# Паддинг `#trace` — по столбцу с каждой стороны (`styles.tcss`). Нужен там,
# где ширина берётся из события `Resize`, а не замером контейнера.
_TRACE_PADDING = 2
# Ширина, на которую собрано тело диффа, ещё не мерилась. Отдельное значение:
# `None` — законная ширина «раскладки ещё нет», и совпадение с ней означало бы
# «тело актуально», хотя его не собирали (приём вьюера правок).
_DIFF_UNBUILT = -1


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

    DEFAULT_CSS = """
    ToolCallDetailScreen .detail-output { height: auto; width: 1fr; }
    ToolCallDetailScreen .detail-diff {
        height: auto; width: 1fr; text-wrap: nowrap; text-overflow: clip;
    }
    """

    def __init__(
        self,
        *,
        call_event: TraceEvent,
        result_event: Optional[TraceEvent],
        edit_record: Optional[EditRecord] = None,
    ) -> None:
        super().__init__()
        self._call_event = call_event
        self._result_event = result_event
        self._edit_record = edit_record
        self._fill = _DIFF_UNBUILT  # ширина, на которую собрана заливка

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

    def on_resize(self, event: events.Resize) -> None:
        """Ширина сменилась — заливка строк диффа тянется до нового края карточки.

        Сверка по ширине заливки обязательна: `Resize` приходит и на смену
        высоты, а от неё дифф не зависит вовсе. Ширина берётся ИЗ СОБЫТИЯ:
        экран получает `Resize` раньше, чем `#trace` примет новый размер, и
        замер контейнера отдал бы предыдущую ширину — заливка отставала бы от
        рамки на шаг (тот же приём, что во вьюере правок).
        """
        if self._edit_record is None:
            return
        fill = self._fill_width(event.size.width)
        if fill == self._fill:
            return
        self._fill = fill
        for widget in self.query(".detail-diff"):
            widget.update(self._diff_text(fill))

    def _fill_width(self, screen_width: Optional[int] = None) -> Optional[int]:
        """Ширина заливки строки: тело `#trace` минус колонка скроллбара.

        Скроллбар резервируется всегда: `size.width` его колонку не вычитает, а
        у карточки события он есть почти наверняка — под диффом лежит ещё и
        полный payload. `None` — ширины ещё нет (`compose` до раскладки): движок
        зальёт по содержимому, первый же `Resize` пересоберёт.
        """
        if screen_width is None:
            try:
                width = self.query_one("#trace", VerticalScroll).size.width
            except Exception:
                width = 0
        else:
            width = screen_width - _TRACE_PADDING
        width -= 1
        return width if width > 0 else None

    def _diff_text(self, fill: Optional[int]):
        record = self._edit_record
        return render_hunks(
            record.hunks, path=record.path, fill_width=fill
        ).text

    def _header_text(self) -> str:
        ev = self._call_event
        # Экран вызова — место сырого wire-имени (в ленте — подпись).
        tool = ev.wire_name or ev.tool_name or "<tool>"
        return f"Viewer: {tool}({ev.args_summary}) · #{ev.no}"

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
        if isinstance(output_val, str):
            output_val = output_for_card(output_val)
        if output_val is None:
            return Static("(пустой output)", classes="line-meta", markup=False)
        if self._edit_record is not None:
            return self._edit_output_widget(output_val)
        return render_payload(output_val)

    def _edit_output_widget(self, output_val) -> Vertical:
        """Правка агента: дифф движком целиком, дальше остальной вывод.

        Сырое поле `diff` из payload убирается — те же хэнки, только нечитаемой
        JSON-строкой; всё прочее (path, счётчики, message) остаётся как было.
        """
        record = self._edit_record
        render = render_hunks(record.hunks, path=record.path)
        rest = unfold_payload(output_val)
        if isinstance(rest, dict):
            rest = {k: v for k, v in rest.items() if k != "diff"}
        widgets = []
        if render.total_lines:
            # Ширины на `compose` ещё нет — заливка по содержимому; правый край
            # доводит первый же `Resize` (он приходит сразу после раскладки).
            widgets.append(
                Static(render.text, classes="detail-diff", markup=False)
            )
        widgets.append(render_payload(rest))
        return Vertical(*widgets, classes="detail-output")
