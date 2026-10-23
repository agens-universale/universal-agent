"""Исполнитель решений модели блоков ленты (спека P4 §8.1).

Тонкий и не виджет: события строк вызовов подаёт в модель уровня
(`gc_tui.services.tool_blocks.ToolBlocks`), её решения применяет к виджетам
через хост монтирования (`BlockHost.mount`) — живая лента подаёт обёртку
над своим монтированием, превью — над целью превью. Виды цвета модели
переводит в токены палитры. Свои виджеты (заголовки, фразы, строки, диффы)
регистрирует до монтирования: точка закрытия групп по `issued` видит, что
виджет выдан моделью, и группу им не закрывает.

Фразы блока (`BlockAction`) — прямые дети ленты, клик шлёт `BlockToggle`
с уровнем: одинаковые ключи `block:<n>` разных уровней не путаются,
маршрут — `BlockViews.route`.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol

from rich.text import Text
from textual import events
from textual.message import Message
from textual.widget import Widget

from gc_tui.models import TraceEvent
from gc_tui.palette import DIM, ERR, INK, MUTED, OK, REFUSED, RUN_COLOR
from gc_tui.services.block_edits import diff_block_for
from gc_tui.services.tool_blocks import (
    BlockEdit,
    Decision,
    MountHeader,
    MountPhrase,
    Parts,
    SetConsequence,
    SetDiffShare,
    SetEditTail,
    SetExpanded,
    SetHeader,
    SetMode,
    SetPhrase,
    SetTimer,
    SetVisible,
    SetWord,
    ToolBlocks,
    diff_key,
    phrase_key,
)
from gc_tui.services.trace_render import level_prefix
from gc_tui.widgets.no_select import NoSelectStatic
from gc_tui.widgets.tool_call_row import ToolCallRow

#: Вид цвета модели → токен палитры.
_LOOK: dict[str, str] = {
    "run": RUN_COLOR,
    "ok": OK,
    "error": ERR,
    "refused": REFUSED,
    "halted": DIM,
    "dim": DIM,
    "ink": INK,
    "muted": MUTED,
}
#: Класс скрытого виджета блока (`display: none`): вне окна, ждущий.
HIDDEN_CLASS = "-block-hidden"


def _text(parts: Parts) -> Text:
    text = Text()
    for chunk, look in parts:
        text.append(chunk, style=_LOOK[look])
    return text


class BlockHost(Protocol):
    """Куда исполнитель монтирует свои виджеты; удаления нет (§9.4)."""

    def mount(
        self,
        widget: Widget,
        *,
        before: Optional[Widget] = None,
        after: Optional[Widget] = None,
    ) -> None: ...


class BlockToggle(Message):
    """Раскрыть / свернуть блок ``key`` модели уровня ``level`` (`""` —
    верхний уровень, иначе `parent_call_id` субагента)."""

    def __init__(self, level: str, key: str) -> None:
        super().__init__()
        self.level = level
        self.key = key


class BlockAction(NoSelectStatic):
    """Фраза блока: `… ещё N`, `+ ещё N строк`, `− свернуть`. Маркер окна
    над членами — с `└ ` в колонке 2, прочие — в колонке 4; клик — тогл.
    Строка незаписанных файлов кликом ничего не делает."""

    DEFAULT_CSS = "BlockAction { height: auto; width: 1fr; }"
    can_focus = False

    def __init__(
        self,
        *,
        level: str,
        key: str,
        depth: int,
        lead: bool,
        clickable: bool = True,
    ) -> None:
        classes = "trace-action block-action" if clickable else "block-note"
        super().__init__("", classes=classes, markup=False)
        self.level = level
        self.key = key
        self.phrase = ""
        self._depth = depth
        self._lead = lead
        self._clickable = clickable

    def set_phrase(self, phrase: str) -> None:
        self.phrase = phrase
        text = Text(level_prefix(self._depth + 1))
        text.append("└ " if self._lead else "  ", style=DIM)
        # Цвет фразы — от CSS (`.block-action`, ховер `.trace-action:hover`).
        text.append(phrase)
        self.update(text)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if self._clickable:
            self.post_message(BlockToggle(self.level, self.key))


class ToolBlocksView:
    """Применяет решения модели одного уровня к виджетам ленты."""

    def __init__(
        self,
        host: BlockHost,
        model: ToolBlocks,
        *,
        level: str,
        depth: int,
    ) -> None:
        self.model = model
        self.level = level
        self.depth = depth
        self._host = host
        self._widgets: dict[str, Widget] = {}
        self._issued: set[Widget] = set()
        self._edits: dict[str, BlockEdit] = {}
        self._compact: set[str] = set()

    # --- выданные виджеты --------------------------------------------------

    def issued(self, widget: Widget) -> bool:
        """Виджет выдан моделью блоков — группу он не закрывает."""
        return widget in self._issued

    def register(self, key: str, widget: Widget) -> None:
        self._widgets[key] = widget
        self._issued.add(widget)

    # --- события строк: событие → модель → решения -------------------------

    def add_call(self, row: ToolCallRow, category, *, waiting: bool) -> None:
        row.block_key = (self.level, row.call_id)
        self.register(row.call_id, row)
        self._host.mount(row)
        self.apply(
            self.model.member_added(row.call_id, category, waiting=waiting)
        )

    def started(self, row: ToolCallRow) -> None:
        row.set_active()
        self.apply(self.model.started(row.call_id))

    def result(
        self,
        row: ToolCallRow,
        ev: TraceEvent,
        edit: Optional[BlockEdit],
        *,
        compact: bool = False,
    ) -> None:
        """Исход вызова вместе с правкой: её глагол решает группу."""
        key = row.call_id
        if edit is not None:
            self._edits[key] = edit
            if compact:
                self._compact.add(key)
            # Хвост файла задаёт модель; строка берёт только ❐.
            row.set_edit_tail(Text())
            row.set_edit_counts(edit.add, edit.rem, copy_text=edit.copy_text)
        outcome = ev.result_outcome
        row.set_result(summary=ev.summary, outcome=outcome)
        self.apply(
            self.model.result(
                key, outcome, reason=ev.summary, executed=None, edit=edit
            )
        )

    def interrupted(self, row: ToolCallRow, *, started: bool) -> None:
        row.mark_interrupted()
        self.apply(self.model.interrupted(row.call_id, started=started))

    def paused(self, row: ToolCallRow) -> None:
        row.pause()
        self.apply(self.model.paused(row.call_id))

    def resumed(self, row: ToolCallRow) -> None:
        row.resume()
        self.apply(self.model.resumed(row.call_id))

    def toggle(self, key: str) -> None:
        self.apply(self.model.toggle(key))

    # --- решения → виджеты ---------------------------------------------------

    def apply(self, decisions: list[Decision]) -> None:
        for decision in decisions:
            _HANDLERS[type(decision)](self, decision)

    def _anchor(self, key: Optional[str]) -> Optional[Widget]:
        """Виджет ключа; диффа нет (записи не было) — его строка."""
        if key is None:
            return None
        widget = self._widgets.get(key)
        if widget is None and key.endswith(":diff"):
            widget = self._widgets.get(key.removesuffix(":diff"))
        return widget

    def _mount_header(self, d: MountHeader) -> None:
        if d.block in self._widgets:
            return
        header = ToolCallRow(
            call_id=f"{self.level}/{d.block}",
            label="",
            target="",
            level=self.depth,
        )
        header.set_mode("header", lead=False)
        header.block_key = (self.level, d.block)
        header.set_detail_source(_last_call(self.model, d.block))
        self.register(d.block, header)
        self._host.mount(header, before=self._anchor(d.before))

    def _mount_phrase(self, d: MountPhrase) -> None:
        key = phrase_key(d.block, d.slot)
        before, after = self._anchor(d.before), self._anchor(d.after)
        phrase = self._widgets.get(key)
        if phrase is not None:
            # Уже стоит — переставить под новый последний виджет блока.
            if phrase.parent is not None and (before or after) is not phrase:
                phrase.parent.move_child(phrase, before=before, after=after)
            return
        phrase = BlockAction(
            level=self.level,
            key=key,
            depth=self.depth,
            lead=d.slot == "top",
            clickable=d.slot != "unrecorded",
        )
        self.register(key, phrase)
        self._host.mount(phrase, before=before, after=after)

    def _set_mode(self, d: SetMode) -> None:
        widget = self._widgets.get(d.key)
        if isinstance(widget, ToolCallRow):
            widget.set_mode(d.mode, lead=d.lead, current=d.current, gap=d.gap)

    def _set_visible(self, d: SetVisible) -> None:
        for key in (d.key, diff_key(d.key)):
            widget = self._widgets.get(key)
            if widget is not None:
                widget.set_class(not d.visible, HIDDEN_CLASS)
        widget = self._widgets.get(d.key)
        if not d.visible and widget is not None and widget.has_focus:
            header = self._widgets.get(self.model.block_of(d.key) or "")
            if header is not None:
                header.focus()

    def _set_header(self, d: SetHeader) -> None:
        header = self._widgets[d.block]
        assert isinstance(header, ToolCallRow)
        header.set_header(d.word, glyph=d.glyph, tail=_text(d.tail))

    def _set_phrase(self, d: SetPhrase) -> None:
        phrase = self._widgets.get(phrase_key(d.block, d.slot))
        if not isinstance(phrase, BlockAction):
            return
        phrase.set_class(d.text is None, HIDDEN_CLASS)
        if d.text is not None:
            phrase.set_phrase(d.text)

    def _row(self, key: str) -> Optional[ToolCallRow]:
        widget = self._widgets.get(key)
        return widget if isinstance(widget, ToolCallRow) else None

    def _set_timer(self, d: SetTimer) -> None:
        row = self._row(d.key)
        if row is not None:
            row.set_elapsed(d.seconds, live=d.live)

    def _set_consequence(self, d: SetConsequence) -> None:
        row = self._row(d.key)
        if row is not None:
            row.set_consequence(Text(d.text, style=_LOOK[d.look]))

    def _set_word(self, d: SetWord) -> None:
        row = self._row(d.key)
        if row is not None:
            row.set_word(d.word)

    def _set_edit_tail(self, d: SetEditTail) -> None:
        row = self._row(d.key)
        if row is not None:
            row.set_edit_tail(_text(d.parts))

    def _set_diff_share(self, d: SetDiffShare) -> None:
        """Дифф файла: смонтировать при первой доле, дальше — перерасчёт
        доли и колонки без перемонтирования."""
        key = diff_key(d.key)
        diff = self._widgets.get(key)
        if diff is None:
            edit = self._edits.get(d.key)
            owner = self._widgets.get(d.key)
            if edit is None or edit.record is None or owner is None:
                return
            diff = diff_block_for(
                edit.record,
                rows=d.rows or 0,
                column=d.column,
                compact=d.key in self._compact,
            )
            self.register(key, diff)
            self._host.mount(diff, after=owner)
        diff.set_expanded(d.rows is None)
        if d.rows is not None:
            diff.set_collapsed_max_lines(d.rows)
        diff.set_column(d.column)

    def _set_expanded(self, d: SetExpanded) -> None:
        """Раскрытие частей `Ran` (команда, вывод) — их виджеты появятся с
        превью вывода команды; до тех пор раскрывать нечего."""


def _last_call(model: ToolBlocks, block: str) -> Callable[[], Optional[str]]:
    return lambda: model.last_call(block)


_HANDLERS: dict[type, Callable[[ToolBlocksView, Decision], None]] = {
    MountHeader: ToolBlocksView._mount_header,
    MountPhrase: ToolBlocksView._mount_phrase,
    SetMode: ToolBlocksView._set_mode,
    SetVisible: ToolBlocksView._set_visible,
    SetHeader: ToolBlocksView._set_header,
    SetPhrase: ToolBlocksView._set_phrase,
    SetTimer: ToolBlocksView._set_timer,
    SetConsequence: ToolBlocksView._set_consequence,
    SetWord: ToolBlocksView._set_word,
    SetEditTail: ToolBlocksView._set_edit_tail,
    SetDiffShare: ToolBlocksView._set_diff_share,
    SetExpanded: ToolBlocksView._set_expanded,
}


class BlockViews(dict[str, ToolBlocksView]):
    """Исполнители уровней ленты: `""` — верхний, иначе `parent_call_id`."""

    def route(self, message: BlockToggle) -> None:
        """Тогл — в модель своего уровня."""
        view = self.get(message.level)
        if view is not None:
            view.toggle(message.key)
