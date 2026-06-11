"""TraceRow — двух-колоночный layout (prefix + content) для строк трассы.

Главное архитектурное преимущество: content рендерится в Static с шириной 1fr,
Textual wrap'ит его внутри этой колонки. Wrapped-строки автоматически
начинаются с левого края content-колонки = выравниваются под началом
контента первой строки = hanging indent без manual textwrap.

Используется для всех типов строк трассы кроме:
- user reply (UserReplyContainer / band)
- fenced code-block panel (AgentReplyLine + rich.syntax)
- tool-вызовов (ToolCallRow — собственный compose, тоже Horizontal-based)
"""
from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static


class TraceRow(Widget):
    """Строка трассы: prefix-колонка фиксированной ширины + content-колонка с wrap.

    DOM:
        TraceRow.{kind_class}
            Horizontal
                Static(prefix, classes="trace-prefix")
                Static(content, classes="trace-content")

    kind_class применяется ТОЛЬКО к самому TraceRow, чтобы CSS-правила
    (margin-top и т.п.) двигали всю строку целиком — prefix и content
    остаются выровненными. Если бы kind_class дублировался на content-Static,
    margin-top сработал бы дважды (на ряду и на content внутри Horizontal),
    и content уезжал бы на строку ниже prefix'а.

    Color/text-style из .line-* правил каскадируются на внутренний Static
    через Textual rich_style (parent → child наследование).

    event_no=0 означает «нет привязанного события трассы» — клик не открывает payload.

    Контракт focus-handler: если в `_on_focus`/`_on_blur` synchronously читаешь
    `self.has_focus` — вызови `super()` ПЕРВЫМ, иначе прочитаешь stale Reactive-
    значение (Widget._on_focus ставит has_focus=True, MRO dispatcher вызывает
    subclass-handler раньше parent-handler).
    """

    DEFAULT_CSS = """
    TraceRow {
        height: auto;
        width: 1fr;
        padding: 0;
    }
    TraceRow > Horizontal {
        height: auto;
        width: 1fr;
    }
    """
    can_focus = True

    def __init__(
        self,
        *,
        prefix: str,
        content,
        kind_class: str,
        event_no: int = 0,
        focusable: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._prefix = prefix
        self._content = content
        self._kind_class = kind_class
        self._event_no = event_no
        # Apply kind_class to TraceRow itself so CSS rules (margin-top etc.)
        # push the whole row, keeping prefix-Static and content-Static aligned.
        for cls in kind_class.split():
            self.add_class(cls)
        # Instance-level override класс-атрибута can_focus.
        # Tab-цикл = «1 событие = 1 Tab-стоп»; continuation prose, `* note:`,
        # code-block-панели после первой — focusable=False.
        self.can_focus = focusable

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static(
                self._prefix,
                classes="trace-prefix",
                markup=False,
            )
            yield Static(
                self._content,
                classes="trace-content",
                markup=False,
            )

    def on_click(self, event: events.Click) -> None:
        if event.chain == 2 and self._event_no:
            from tui.messages import OpenEventRequest
            self.post_message(OpenEventRequest(self._event_no))

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter" and self._event_no:
            event.stop()
            from tui.messages import OpenEventRequest
            self.post_message(OpenEventRequest(self._event_no))


class SummaryMarker(TraceRow):
    """Маркер «контекст сжат» И тело сводки: клик или Enter постит
    SummaryToggleRequest, чтобы TraceScreen раскрывал/сворачивал блок.
    Используется и для строки-маркера, и для тела (клик по любому — toggle)."""

    def on_click(self, event: events.Click) -> None:
        event.stop()
        from tui.messages import SummaryToggleRequest
        self.post_message(SummaryToggleRequest(self._event_no))

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            from tui.messages import SummaryToggleRequest
            self.post_message(SummaryToggleRequest(self._event_no))
