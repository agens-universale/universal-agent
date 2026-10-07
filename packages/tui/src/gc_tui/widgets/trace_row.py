"""TraceRow — двух-колоночный layout (prefix + content) для строк трассы.

Главное архитектурное преимущество: content рендерится в Static с шириной 1fr,
Textual wrap'ит его внутри этой колонки. Wrapped-строки автоматически
начинаются с левого края content-колонки = выравниваются под началом
контента первой строки = hanging indent без manual textwrap.

Используется для всех типов строк трассы кроме:
- user reply (UserReplyContainer / band)
- fenced code-block panel (BlockPanel)
- tool-вызовов (ToolCallRow — собственный compose, тоже Horizontal-based)
"""

from __future__ import annotations

from typing import Optional

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from gc_tui.widgets.click_model import ClickModelMixin, confirm_is_active


class TraceRow(ClickModelMixin, Widget):
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
        self._event_no = event_no
        # Apply kind_class to TraceRow itself so CSS rules (margin-top etc.)
        # push the whole row, keeping prefix-Static and content-Static aligned.
        for cls in kind_class.split():
            self.add_class(cls)
        # Instance-level override класс-атрибута can_focus.
        # Tab-цикл = «1 событие = 1 Tab-стоп»; continuation prose, `* note:`,
        # code-block-панели после первой — focusable=False.
        self.can_focus = focusable

    def set_prefix(self, prefix: str) -> None:
        """Обновить prefix-колонку после монтирования (напр. стрелка ▸/▾)."""
        self._prefix = prefix
        try:
            self.query_one(".trace-prefix", Static).update(prefix)
        except Exception:
            pass

    def set_content(self, content, *, layout: bool = True) -> None:
        """Обновить content-колонку после монтирования (пер-тик апдейт стрим-прозы).
        `layout=False` — размер не меняется (перекраска), ленту не перекомпоновывать."""
        self._content = content
        try:
            self.query_one(".trace-content", Static).update(
                content, layout=layout
            )
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield self._make_prefix()
            yield self._make_content()

    def _make_prefix(self) -> Widget:
        """Виджет prefix-колонки (класс `trace-prefix`); подкласс может заменить."""
        return Static(self._prefix, classes="trace-prefix", markup=False)

    def _make_content(self) -> Widget:
        """Виджет content-колонки (класс `trace-content`); подкласс может заменить."""
        return Static(self._content, classes="trace-content", markup=False)

    def _open_request(self) -> Optional[Message]:
        if not self._event_no:
            return None
        from gc_tui.messages import OpenEventRequest

        return OpenEventRequest(self._event_no)


class SummaryMarker(TraceRow):
    """Маркер «контекст сжат» И тело сводки: клик или Enter постит
    SummaryToggleRequest, чтобы TraceScreen раскрывал/сворачивал блок.
    Используется и для строки-маркера, и для тела (клик по любому — toggle)."""

    def on_click(self, event: events.Click) -> None:
        # Симметрия с on_key: при security-confirm клик не тогглит сводку —
        # раскрытое тело съезжает под баннер, а жест пропадает впустую.
        if confirm_is_active(self):
            return
        event.stop()
        from gc_tui.messages import SummaryToggleRequest

        self.post_message(SummaryToggleRequest(self._event_no))

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            # Как ClickModelMixin: при security-confirm Enter — ответ баннеру,
            # не toggle сводки (focused-строка получает Key раньше Screen).
            if confirm_is_active(self):
                return
            event.stop()
            from gc_tui.messages import SummaryToggleRequest

            self.post_message(SummaryToggleRequest(self._event_no))
