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

from collections.abc import Mapping
from typing import Optional

from rich.cells import cell_len
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
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


class CompactionRule(SummaryMarker):
    """Метка сжатия — одна строка во всю ширину ленты (спека P3 §2.5).

    Строку собирает `trace_render.compaction_rule` под текущую ширину:
    пересборка на resize, как у тела `DiffBlock`; `set_expanded` меняет
    стрелку ▸/▾."""

    def __init__(
        self,
        *,
        num: Optional[int],
        comp: Mapping,
        event_no: int,
        focusable: bool,
    ) -> None:
        super().__init__(
            prefix="",
            content="",
            kind_class="summary-marker",
            event_no=event_no,
            focusable=focusable,
        )
        self._num = num
        self._comp = comp
        self._expanded = False
        # До раскладки ширина неизвестна; первый resize пересоберёт.
        self._content = self._rule(0)

    def compose(self) -> ComposeResult:
        yield self._make_content()

    def _rule(self, width: int):
        from gc_tui.services.trace_render import compaction_rule

        return compaction_rule(
            self._num, self._comp, width, expanded=self._expanded
        )

    def rule_text(self):
        """Текущая строка метки (rich Text)."""
        return self._content

    def _sync(self) -> None:
        self.set_content(self._rule(self.size.width), layout=False)

    def on_mount(self) -> None:
        self._sync()

    def on_resize(self, event: events.Resize) -> None:
        if cell_len(self._content.plain) != self.size.width:
            self._sync()

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self.set_class(expanded, "expanded")
        self._sync()


class CompactionBody(SummaryMarker):
    """Тело сводки под меткой сжатия, свёрнуто по умолчанию: сколько
    сообщений ушло в сводку, сам текст и `− свернуть` (класс ховера
    `.trace-action`). Клик по телу — тот же `SummaryToggleRequest`."""

    def __init__(
        self, *, messages: Optional[int], text: str, event_no: int
    ) -> None:
        from gc_tui.services.trace_render import PROSE_BULLET

        super().__init__(
            # Текст тела — в колонке прозы реплики, под подписью `●`.
            prefix=" " * len(PROSE_BULLET),
            content=text,
            kind_class="summary-body hidden",
            event_no=event_no,
            focusable=False,
        )
        self._head = _compaction_head(messages)
        self._text = text

    def _make_content(self) -> Widget:
        from gc_tui.widgets.no_select import NoSelectStatic

        return Vertical(
            Static(self._head, classes="compaction-head", markup=False),
            Static(self._text, classes="compaction-text", markup=False),
            NoSelectStatic(
                COMPACTION_COLLAPSE,
                classes="compaction-collapse trace-action",
                markup=False,
            ),
            classes="compaction-content",
        )


COMPACTION_COLLAPSE = "− свернуть"


def _compaction_head(messages: Optional[int]) -> str:
    """«Контекст сохранён: 34 сообщения → сводка»; без счёта — без числа."""
    if not messages:
        return "Контекст сохранён: сводка"
    from gc_tui.services.plural import plural_form

    word = plural_form(messages, "сообщение", "сообщения", "сообщений")
    return f"Контекст сохранён: {messages} {word} → сводка"


class QuestionRow(TraceRow):
    """Вопрос ask_human в ленте (серая рамка `? …`) со своими следствиями.

    `call_id` — по нему ответ «без ответа» находит свой вопрос. Следствие
    (`└ без ответа`, `└ прервано …`) — строкой внутри рамки: виджет события
    один (спека P3 §3, решение плана 2)."""

    def __init__(self, *, call_id: Optional[str] = None, **kwargs) -> None:
        from gc_tui.widgets.consequence_line import ConsequenceSlot

        super().__init__(**kwargs)
        self.call_id = call_id
        self._consequences: list = []
        self._consequence_slot = ConsequenceSlot(0)

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield self._consequence_slot

    def add_consequence(self, body) -> None:
        """Дописать строку `└ body` под текстом вопроса, внутри рамки."""
        self._consequences.append(body)
        self._consequence_slot.set_bodies(self._consequences)
