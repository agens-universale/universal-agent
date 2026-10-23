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

from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Sequence

from rich.text import Text
from textual import events
from textual.widget import Widget

from gc_tui.messages import BlockToggle
from gc_tui.models import CommandOutput, TraceEvent
from gc_tui.palette import DIM, ERR, INK, MUTED, OK, REFUSED, RUN_COLOR
from gc_tui.services.block_edits import diff_block_for
from gc_tui.services.footer_slot import RETENTION_DETAIL_HINT
from gc_tui.services.tool_blocks import (
    AGENT_WINDOW,
    COMMAND_PREVIEW_LINES,
    BlockEdit,
    BlockKind,
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
    command_file_verb,
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


@dataclass(frozen=True)
class RanResult:
    """Что знает лента о законченной команде: разобранный вывод, правки
    команды из журнала, незаписанные файлы сверх потолка путей."""

    output: Optional[CommandOutput]
    files: Sequence[BlockEdit] = ()
    unrecorded: int = 0
    #: Запись вне окна retention: всего вывода при раскрытии нет.
    light: bool = False
    #: Ключи файлов с компактным диффом (ход вне раскрытых).
    compact: frozenset[str] = field(default_factory=frozenset)


def _lines_text(head: Optional[Text], lines: Sequence[str]) -> Text:
    text = Text()
    if head is not None:
        text.append_text(head)
    for line in lines:
        if text.plain:
            text.append("\n")
        text.append(line, style=DIM)
    return text


def output_texts(ran: RanResult) -> Optional[tuple[Text, Text]]:
    """Следствие `Ran` (§4.1): свёрнутое и раскрытое. Первая строка —
    код выхода или таймаут (`ERR`) сверх превью; фон — одна строка; нет
    вывода — `(нет вывода)`. Вне окна retention раскрытие — превью и
    подсказка. Не конверт exec — следствия от вывода нет."""
    out = ran.output
    if out is None:
        return None
    if out.background:
        line = Text("запущено в фоне · вывод не сохраняется", style=DIM)
        return line, line
    head = None
    if out.timed_out:
        head = Text("превышено время ожидания", style=ERR)
    elif out.exit_code not in (None, 0):
        head = Text(f"код выхода {out.exit_code}", style=ERR)
    if head is None and not out.lines:
        line = Text("(нет вывода)", style=DIM)
        return line, line
    collapsed = _lines_text(head, out.preview)
    if ran.light:
        full = (*out.preview, RETENTION_DETAIL_HINT)
        return collapsed, _lines_text(head, full)
    return collapsed, _lines_text(head, out.lines)


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


class BlockAction(NoSelectStatic):
    """Фраза блока: `… ещё N`, `+ ещё N строк`, `− свернуть`. Маркер окна
    над членами — с `└ ` в колонке 2, прочие — в колонке 4; клик — тогл.
    Строка незаписанных файлов кликом ничего не делает."""

    DEFAULT_CSS = "BlockAction { height: auto; width: 1fr; }"
    can_focus = False
    # Клик по фразе фокус не двигает: ни на ленту, ни на строку
    # (`click_model.focus_target_at`).
    FOCUS_ON_CLICK = False

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
        self._command_hidden: dict[str, int] = {}
        self._event_count = 0

    # --- выданные виджеты --------------------------------------------------

    def issued(self, widget: Widget) -> bool:
        """Виджет выдан моделью блоков — группу он не закрывает."""
        return widget in self._issued

    def register(self, key: str, widget: Widget) -> None:
        self._widgets[key] = widget
        self._issued.add(widget)

    def set_owner(self, row: Widget) -> None:
        """Строка `Agent` уровня субагента: под ней — фраза окна."""
        self._widgets[AGENT_WINDOW] = row

    def event_added(self, widget: Widget) -> None:
        """Смонтированное событие уровня не из блоков (проза, чек-лист,
        реплика): закрывает группу; у субагента — событие окна."""
        key = f"event:{self._event_count}"
        self._event_count += 1
        self._widgets[key] = widget
        self.apply(self.model.event_added(key))

    # --- события строк: событие → модель → решения -------------------------

    def add_call(
        self,
        row: ToolCallRow,
        category,
        *,
        waiting: bool,
        command: tuple[str, ...] = (),
    ) -> None:
        """Строка вызова; ``command`` — команда `Ran` построчно: длинная
        раскрывается и пока идёт (§4.0)."""
        row.block_key = (self.level, row.call_id)
        self.register(row.call_id, row)
        self._host.mount(row)
        key = row.call_id
        self.apply(self.model.member_added(key, category, waiting=waiting))
        if self.model.kind_of(key) != BlockKind.RAN or not command:
            return
        row.set_command(command)
        hidden = max(0, len(command) - COMMAND_PREVIEW_LINES)
        self._command_hidden[key] = hidden
        if self._command_hidden[key]:
            self._ran_parts(key, RanResult(output=None))

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
        ran: Optional[RanResult] = None,
    ) -> None:
        """Исход вызова вместе с правкой: её глагол решает группу; у
        `Ran` — вывод и правки команды (``ran``)."""
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
        if ran is not None and self.model.kind_of(key) == BlockKind.RAN:
            self._ran_result(row, ran)

    def _ran_result(self, row: ToolCallRow, ran: RanResult) -> None:
        """Вывод — следствием строки; файлы команды — строками под ней (до
        решений модели: окно и фразы ставятся по ним)."""
        texts = output_texts(ran)
        if texts is not None:
            row.set_output(*texts)
        anchor: Widget = row
        for file in ran.files:
            anchor = self._mount_file(row, file, after=anchor)
            if file.key in ran.compact:
                self._compact.add(file.key)
        self._ran_parts(row.call_id, ran)

    def _mount_file(
        self, row: ToolCallRow, file: BlockEdit, *, after: Widget
    ) -> ToolCallRow:
        """Строка файла под командой: `Added / Edited / Deleted путь`."""
        line = ToolCallRow(
            call_id=file.key,
            label=file.verb,
            target=file.path,
            event_no=row.event_no,
            level=self.depth,
            path_target=True,
        )
        line.block_key = (self.level, row.call_id)
        # Enter на строке файла — экран деталей самой команды.
        line.set_detail_source(lambda: row.call_id)
        line.set_word(file.verb)
        line.set_result(summary="", outcome="ok")
        if file.verb != command_file_verb("delete") and file.copy_text:
            line.set_edit_counts(file.add, file.rem, copy_text=file.copy_text)
        self._edits[file.key] = file
        self.register(file.key, line)
        self._host.mount(line, after=after)
        return line

    def _ran_parts(self, key: str, ran: RanResult) -> None:
        output = ran.output
        self.apply(
            self.model.ran_parts(
                key,
                command_hidden=self._command_hidden.get(key, 0),
                output_hidden=output.hidden if output is not None else 0,
                files=ran.files,
                unrecorded=ran.unrecorded,
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

    def event_closing(self) -> None:
        self.apply(self.model.event_closing())

    def queued_admitted(self) -> None:
        self.apply(self.model.queued_admitted())

    def timers_updated(self) -> None:
        self.apply(self.model.timers_updated())

    def head_widget(self, key: str) -> Optional[Widget]:
        """Верх блока ключа ``key`` (блока, фразы или члена): заголовок, у
        блока без заголовка — его первая строка."""
        block = self.model.block_of(key)
        if block is None:
            return None
        head = self._widgets.get(block)
        if head is None:
            head = self._widgets.get(self.model.first_call(block) or "")
        return head

    def turn_ended(self, *, stopped: bool) -> None:
        """Запись итога хода; ``stopped`` — ход остановлен пользователем."""
        if stopped:
            self.apply(self.model.turn_stopped())
        self.apply(self.model.turn_ended())

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
        # Фраза окна субагента — в колонке глифа его событий (§7).
        window = d.block == AGENT_WINDOW
        phrase = BlockAction(
            level=self.level,
            key=key,
            depth=self.depth - 1 if window else self.depth,
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
            row.set_elapsed(d.seconds)

    def _set_consequence(self, d: SetConsequence) -> None:
        row = self._row(d.key)
        if row is not None:
            row.set_consequence(Text(d.text, style=_LOOK[d.look]))

    def _set_word(self, d: SetWord) -> None:
        row = self._row(d.key)
        if row is not None:
            row.set_word(d.word, role=d.role)

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
                event_no=getattr(owner, "event_no", 0),
            )
            # Колонка субагента — на 2 глубже; видимость — как у строки.
            diff.set_class(self.depth > 0, "diff-nested")
            diff.set_class(owner.has_class(HIDDEN_CLASS), HIDDEN_CLASS)
            self.register(key, diff)
            self._host.mount(diff, after=owner)
        diff.set_expanded(d.rows is None)
        if d.rows is not None:
            diff.set_collapsed_max_lines(d.rows)
        diff.set_column(d.column)

    def _set_expanded(self, d: SetExpanded) -> None:
        """Раскрытие частей `Ran`: вся команда и весь вывод."""
        row = self._row(self.model.first_call(d.block) or "")
        if row is not None:
            row.set_parts_expanded(d.expanded)


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

    def route(
        self, message: BlockToggle
    ) -> Optional[tuple[ToolBlocksView, str]]:
        """Тогл — в модель своего уровня; строка `Agent` раскрывает окно
        своего субагента (§7). Возвращает исполнитель и ключ тогла."""
        view = self.get(message.level)
        if view is None:
            return None
        key = message.key
        if view.model.kind_of(key) == BlockKind.AGENT:
            agent = view.model.first_call(view.model.block_of(key) or "")
            nested = self.get(agent or "")
            if nested is None:
                return None
            view, key = nested, AGENT_WINDOW
        view.toggle(key)
        return view, key
