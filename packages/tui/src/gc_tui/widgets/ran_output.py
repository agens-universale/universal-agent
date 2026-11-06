"""Части строки `Ran` под командой (спека P4 §4.0, §4.1, решение
2026-10-26): раскрывают там, где скрытое, сворачивают внизу.

Сверху вниз: фраза команды — под её продолжением (`+ ещё K строк`,
раскрыта — `− свернуть` там же); первая строка вывода `└ код выхода N`
(таймаут) — сверх превью; фраза `… ещё N строк` — там, где вывод
начинается; хвост вывода (свёрнутый) или весь вывод (раскрытый). `└` — у
первой видимой строки вывода, прочие — в колонке текста. Команда и вывод
раскрываются независимо (решение 2026-10-27); `− свернуть` раскрытого
вывода — фраза блока под строкой.

Виджеты частей создаются один раз и дальше обновляются на месте:
раскрытие и свёртка ложатся одним кадром, а растущий вывод (стрим)
просто сдвигает хвост."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from gc_tui.services.trace_render import consequence_lead, level_prefix
from gc_tui.widgets.no_select import NoSelectStatic
from gc_tui.widgets.static_content import update_if_changed

#: Класс фразы раскрытия в строке `Ran`: клик ловит строка.
RAN_PHRASE_CLASS = "ran-phrase"


@dataclass(frozen=True)
class OutputTexts:
    """Вывод `Ran` для строки: ``head`` — код выхода или таймаут (сверх
    превью, `None` — нет), ``tail`` — свёрнутый (хвост; или единственная
    строка: нет вывода, фон), ``full`` — раскрытый."""

    head: Optional[Text]
    tail: Text
    full: Text


class RanPhrase(NoSelectStatic):
    """Фраза раскрытия в строке `Ran`. Клик шлёт тогл ``toggle_key``
    через строку (`ToolCallRow.on_click`); фокус не двигает."""

    DEFAULT_CSS = "RanPhrase { width: 1fr; height: auto; }"
    FOCUS_ON_CLICK = False

    def __init__(self) -> None:
        super().__init__(
            "",
            classes=f"trace-action block-action {RAN_PHRASE_CLASS}",
            markup=False,
        )
        self.toggle_key: Optional[str] = None

    def show(self, lead: Text, phrase: Optional[str]) -> None:
        self.display = phrase is not None
        if phrase is not None:
            text = lead.copy()
            # Цвет фразы — от CSS (`.block-action`, ховер `.trace-action`).
            text.append(phrase)
            update_if_changed(self, text)


class _OutputLine(Horizontal):
    """Строка вывода: отступ с `└ ` (или без) и тело — перенос под телом,
    как у строки следствия."""

    DEFAULT_CSS = """
    _OutputLine { height: auto; width: 1fr; }
    _OutputLine > .consequence-lead { width: auto; height: auto; }
    _OutputLine > .consequence-body { width: 1fr; height: auto; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._lead = NoSelectStatic(
            "", classes="consequence-lead", markup=False
        )
        self._body = Static("", classes="consequence-body", markup=False)

    def compose(self) -> ComposeResult:
        yield self._lead
        yield self._body

    def show(self, lead: Text, body: Optional[Text]) -> None:
        self.display = body is not None
        if body is not None:
            update_if_changed(self._lead, lead)
            update_if_changed(self._body, body)


class RanParts(Vertical):
    """Части строки `Ran` уровня ``level`` под её первой строкой."""

    DEFAULT_CSS = "RanParts { height: auto; width: 1fr; }"

    def __init__(self, level: int) -> None:
        super().__init__()
        self._level = level
        self._command = RanPhrase()
        self._head = _OutputLine()
        self._phrase = RanPhrase()
        self._body = _OutputLine()
        self._texts: Optional[OutputTexts] = None
        self._output_open = False
        self._command_phrase: Optional[str] = None
        self._output_phrase: Optional[str] = None
        self._sync()

    def compose(self) -> ComposeResult:
        yield self._command
        yield self._head
        yield self._phrase
        yield self._body

    def set_output(self, texts: Optional[OutputTexts]) -> None:
        self._texts = texts
        self._sync()

    def set_state(
        self,
        *,
        output_open: bool,
        command: Optional[str],
        output: Optional[str],
        keys: tuple[str, str],
    ) -> None:
        """Раскрыт ли вывод, фразы команды и вывода (`None` — нет) и
        ключи их тоглов."""
        self._output_open = output_open
        self._command_phrase = command
        self._output_phrase = output
        self._command.toggle_key, self._phrase.toggle_key = keys
        self._sync()

    def _sync(self) -> None:
        # Продолжение команды — в колонке текста строки, на 2 от `└`.
        self._command.show(
            Text(level_prefix(self._level + 2)), self._command_phrase
        )
        texts = self._texts
        head = texts.head if texts is not None else None
        phrase = None if texts is None else self._output_phrase
        body = None
        if texts is not None:
            body = texts.full if self._output_open else texts.tail
            body = body if body.plain else None
        first = next((p for p in (head, phrase, body) if p is not None), None)
        self._head.show(self._lead(head is first), head)
        self._phrase.show(self._lead(phrase is first), phrase)
        self._body.show(self._lead(body is first), body)
        self.display = first is not None or self._command_phrase is not None

    def _lead(self, first: bool) -> Text:
        """Отступ части вывода: `└ ` — у первой видимой."""
        if first:
            return consequence_lead(self._level)
        return Text(level_prefix(self._level + 2))
