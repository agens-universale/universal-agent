"""Модель блоков вызовов ленты (спека P4 §2, §3, §8.1) — без Textual.

Лента плоская: заголовок блока, маркер окна, члены и фразы — прямые дети
`#trace`. Модель уровня (верхний или один субагент) получает события
строк вызовов и возвращает решения: что смонтировать и куда, что скрыть,
какой режим у строки, что написать в заголовке. Применяет их исполнитель
(`gc_tui.widgets.tool_blocks_view`); палитры здесь нет — виды глифа и цвета
словами-ключами (`run` / `ok` / `error` / `refused` / `halted` / `dim`).

Решения — разница между желаемым состоянием блока и уже выданным: модель
помнит последнее решение по каждому ключу и повторов не выдаёт.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Mapping, Optional, Union

from gc_core.constants.tool_display import DisplayCategory

from gc_tui.models import ResultOutcome
from gc_tui.services.plural import errors_word, lines_word, refusals_word

#: Видимых элементов в окне блока: членов, файлов, событий субагента.
WINDOW = 3
#: Строк превью вывода команды.
OUTPUT_PREVIEW_LINES = 3
#: Экранных рядов диффа на файл в блоке правок — не меньше.
MIN_DIFF_ROWS_PER_FILE = 3

COLLAPSE_PHRASE = "− свернуть"
NOT_STARTED_REASON = "не запущено (стоп)"

#: Вид глифа заголовка блока.
GlyphLook = Literal["run", "ok", "error", "halted"]
#: Вид цвета куска текста; токен палитры назначает исполнитель.
Look = Literal[
    "run", "ok", "error", "refused", "halted", "dim", "ink", "muted"
]
RowMode = Literal["single", "header", "member"]
Parts = tuple[tuple[str, Look], ...]


class BlockKind(StrEnum):
    EXPLORED = "explored"
    FETCHED = "fetched"
    EDITS = "edits"
    RAN = "ran"
    AGENT = "agent"
    SINGLE = "single"


_KIND_OF: dict[DisplayCategory, BlockKind] = {
    DisplayCategory.READ: BlockKind.EXPLORED,
    DisplayCategory.SEARCH: BlockKind.EXPLORED,
    DisplayCategory.FIND: BlockKind.EXPLORED,
    DisplayCategory.VIEW: BlockKind.EXPLORED,
    DisplayCategory.EDIT: BlockKind.EDITS,
    DisplayCategory.WRITE: BlockKind.EDITS,
    DisplayCategory.FETCH: BlockKind.FETCHED,
    DisplayCategory.RUN: BlockKind.RAN,
    DisplayCategory.AGENT: BlockKind.AGENT,
}
#: Виды, у которых подряд идущие вызовы сворачиваются в одну группу.
_GROUPED = frozenset({BlockKind.EXPLORED, BlockKind.FETCHED})

_WORDS: dict[BlockKind, tuple[str, str]] = {
    BlockKind.EXPLORED: ("Exploring", "Explored"),
    BlockKind.FETCHED: ("Fetching", "Fetched"),
    BlockKind.RAN: ("Running", "Ran"),
    BlockKind.EDITS: ("Editing", "Edited"),
    BlockKind.AGENT: ("Agent", "Agent"),
}

#: Цвет причины `└` по исходу вызова.
_REASON_LOOK: dict[str, Look] = {
    "error": "error",
    "refused": "refused",
    "halted": "dim",
}


def block_kind(category: DisplayCategory) -> Optional[BlockKind]:
    """Вид блока по категории вызова (спека §2.1); `Hidden` — `None`."""
    if category == DisplayCategory.HIDDEN:
        return None
    return _KIND_OF.get(category, BlockKind.SINGLE)


def header_word(
    kind: BlockKind, *, live: bool, verb: Optional[str] = None
) -> str:
    """Слово заголовка: живое (`…ing`) или прошедшее; у правок — глагол
    по результату (`Added` / `Edited`)."""
    going, done = _WORDS[kind]
    if live:
        return going
    return verb or done


def more_phrase(n: int) -> str:
    """Маркер окна над членами: `… ещё N`."""
    return f"… ещё {n}"


def more_lines_phrase(n: int) -> str:
    """Единственная фраза раскрытия, внизу блока: `+ ещё N строк`."""
    return f"+ ещё {n} {lines_word(n)}"


def _failure_parts(errors: int, refusals: int) -> Parts:
    parts: list[tuple[str, Look]] = []
    if errors:
        parts += [(" · ", "dim"), (f"{errors} {errors_word(errors)}", "error")]
    if refusals:
        word = refusals_word(refusals)
        parts += [(" · ", "dim"), (f"{refusals} {word}", "refused")]
    return tuple(parts)


def failure_tail(errors: int, refusals: int) -> str:
    """Счётчик сбоев заголовка: ` · 1 ошибка · 2 отказа`."""
    return "".join(text for text, _ in _failure_parts(errors, refusals))


def block_glyph(*, running: bool, stopped: bool, any_ok: bool) -> GlyphLook:
    """Глиф заголовка (§3.3): стоп — `halted`; идёт — `run`; закончился —
    `ok`, если хоть один вызов прошёл, иначе `error`."""
    if stopped:
        return "halted"
    if running:
        return "run"
    return "ok" if any_ok else "error"


# --- решения -------------------------------------------------------------


@dataclass(frozen=True)
class MountHeader:
    """Смонтировать заголовок блока перед строкой ``before``."""

    block: str
    before: str

    @property
    def ident(self) -> tuple:
        return ("MountHeader", self.block)


@dataclass(frozen=True)
class MountPhrase:
    """Смонтировать фразу ``slot`` блока рядом с виджетом ключа ``after``
    или ``before``; уже смонтированную — переставить туда же."""

    block: str
    slot: str
    after: Optional[str] = None
    before: Optional[str] = None

    @property
    def ident(self) -> tuple:
        return ("MountPhrase", self.block, self.slot)


@dataclass(frozen=True)
class SetMode:
    """Режим строки. ``lead`` — `└ ` перед текстом члена (первый видимый
    элемент блока); ``current`` — член идёт (`INK`)."""

    key: str
    mode: RowMode
    lead: bool = False
    current: bool = False

    @property
    def ident(self) -> tuple:
        return ("SetMode", self.key)


@dataclass(frozen=True)
class SetVisible:
    key: str
    visible: bool

    @property
    def ident(self) -> tuple:
        return ("SetVisible", self.key)


@dataclass(frozen=True)
class SetHeader:
    """Слово, глиф и хвост заголовка блока."""

    block: str
    word: str
    glyph: GlyphLook
    tail: Parts

    @property
    def ident(self) -> tuple:
        return ("SetHeader", self.block)


@dataclass(frozen=True)
class SetPhrase:
    """Текст фразы ``slot``; `None` — фраза скрыта."""

    block: str
    slot: str
    text: Optional[str]

    @property
    def ident(self) -> tuple:
        return ("SetPhrase", self.block, self.slot)


@dataclass(frozen=True)
class SetTimer:
    """Таймер строки. ``live`` — идёт по часам строки; застывший —
    ``seconds`` из записи хода или, если `None`, застывшие часы строки."""

    key: str
    seconds: Optional[float]
    live: bool

    @property
    def ident(self) -> tuple:
        return ("SetTimer", self.key)


@dataclass(frozen=True)
class SetConsequence:
    """Строка `└ text` под вызовом цветом ``look``."""

    key: str
    text: str
    look: Look

    @property
    def ident(self) -> tuple:
        return ("SetConsequence", self.key)


Decision = Union[
    MountHeader,
    MountPhrase,
    SetMode,
    SetVisible,
    SetHeader,
    SetPhrase,
    SetTimer,
    SetConsequence,
]


class CallTimers:
    """Застывшие таймеры вызовов из записей итога хода (`outcome.calls`).

    Один на ленту, общий для моделей всех уровней: записи разных ходов
    сливаются — у них свои `call_id`."""

    def __init__(self) -> None:
        self._calls: dict[str, float] = {}

    def update(self, calls: Mapping[str, float]) -> None:
        self._calls.update(calls)

    def get(self, call_id: str) -> Optional[float]:
        return self._calls.get(call_id)

    def __contains__(self, call_id: object) -> bool:
        return call_id in self._calls

    def clear(self) -> None:
        self._calls.clear()


# --- состояние -----------------------------------------------------------


@dataclass
class _Member:
    key: str
    waiting: bool
    started: bool = False
    outcome: Optional[ResultOutcome] = None
    reason: str = ""
    executed: Optional[bool] = None
    # Без результата: процесс оборвался или стоп до старта.
    interrupted: bool = False
    not_started: bool = False

    @property
    def done(self) -> bool:
        return self.outcome is not None or self.interrupted

    @property
    def live(self) -> bool:
        return not (self.done or self.waiting)

    @property
    def halted(self) -> bool:
        return self.interrupted or self.outcome == "halted"


@dataclass
class _Block:
    key: str
    kind: BlockKind
    members: list[_Member] = field(default_factory=list)
    expanded: bool = False
    stopped: bool = False

    @property
    def headed(self) -> bool:
        return self.kind in _GROUPED

    def shown(self) -> list[_Member]:
        """Члены, которые могут быть видны: ждущие (`•`) — нет."""
        return [m for m in self.members if not m.waiting]

    def window(self) -> list[_Member]:
        shown = self.shown()
        return shown if self.expanded else shown[-WINDOW:]

    def expandable(self) -> bool:
        return self.headed and len(self.shown()) > WINDOW


class ToolBlocks:
    """Модель блоков одного уровня ленты: верхнего или субагента."""

    def __init__(self, timers: CallTimers) -> None:
        self._timers = timers
        self._blocks: list[_Block] = []
        self._block_of: dict[str, _Block] = {}
        self._members: dict[str, _Member] = {}
        self._open: Optional[_Block] = None
        self._issued: dict[tuple, Decision] = {}

    # --- события ---------------------------------------------------------

    def member_added(
        self, call_id: str, category: DisplayCategory, *, waiting: bool
    ) -> list[Decision]:
        """Строка вызова смонтирована: в открытую группу своего вида или
        в новый блок."""
        kind = block_kind(category)
        if kind is None:
            return []
        member = _Member(call_id, waiting=waiting)
        self._members[call_id] = member
        block = self._open
        if block is None or block.kind != kind or kind not in _GROUPED:
            block = self._new_block(kind)
        block.members.append(member)
        self._block_of[call_id] = block
        self._open = block if kind in _GROUPED else None
        return self._render(block)

    def started(self, call_id: str) -> list[Decision]:
        member = self._members.get(call_id)
        if member is None:
            return []
        member.started = True
        member.waiting = False
        return self._render(self._block_of[call_id])

    def result(
        self,
        call_id: str,
        outcome: ResultOutcome,
        *,
        reason: str,
        executed: Optional[bool],
    ) -> list[Decision]:
        """Исход вызова. ``executed`` — исполнялся ли; `None` — решают
        старт строки и реестр таймеров (после переоткрытия)."""
        member = self._members.get(call_id)
        if member is None:
            return []
        member.outcome = outcome
        member.reason = reason
        member.executed = executed
        member.waiting = False
        return self._render(self._block_of[call_id])

    def interrupted(self, call_id: str, *, started: bool) -> list[Decision]:
        """Вызов закончился без результата. ``started=False`` — стоп до
        старта: член с `└ не запущено (стоп)`; иначе (оборвался, или после
        переоткрытия неизвестно) — без `└`."""
        member = self._members.get(call_id)
        if member is None or member.done:
            return []
        member.interrupted = True
        member.not_started = not (started or member.started)
        member.waiting = False
        return self._render(self._block_of[call_id])

    def turn_stopped(self) -> list[Decision]:
        """Ход остановлен: все блоки уровня с незавершёнными членами —
        `halted`, в том числе уже закрытые."""
        out: list[Decision] = []
        for block in self._blocks:
            if any(not m.done for m in block.members):
                block.stopped = True
                out += self._render(block)
        return out

    def paused(self, call_id: str) -> list[Decision]:
        """Ожидание человека: пауза — часы самой строки, блок не меняется."""
        return []

    def resumed(self, call_id: str) -> list[Decision]:
        return []

    def event_closing(self) -> list[Decision]:
        """Событие ленты не из группы закрыло открытую группу."""
        self._open = None
        return []

    def queued_admitted(self) -> list[Decision]:
        """Реплика из очереди впущена ядром — закрывает группу."""
        return self.event_closing()

    def toggle(self, key: str) -> list[Decision]:
        """Раскрыть / свернуть блок по ключу блока, фразы или члена;
        нечего скрывать — решений нет."""
        block = self._block_of.get(key) or self._block_of.get(
            key.rsplit(":", 1)[0]
        )
        if block is None or not block.expandable():
            return []
        block.expanded = not block.expanded
        return self._render(block)

    def timers_updated(self) -> list[Decision]:
        """Реестр таймеров пополнился — застывшие таймеры строкам."""
        out: list[Decision] = []
        for block in self._blocks:
            if any(m.key in self._timers for m in block.members):
                out += self._render(block)
        return out

    # --- отрисовка -------------------------------------------------------

    def _new_block(self, kind: BlockKind) -> _Block:
        block = _Block(f"block:{len(self._blocks)}", kind)
        self._blocks.append(block)
        self._block_of[block.key] = block
        return block

    def _render(self, block: _Block) -> list[Decision]:
        if block.headed:
            wanted = self._grouped(block)
        else:
            wanted = self._single(block)
        return self._diff(wanted)

    def _diff(self, wanted: list[Decision]) -> list[Decision]:
        out: list[Decision] = []
        for decision in wanted:
            ident = decision.ident
            previous = self._issued.get(ident)
            if previous == decision:
                continue
            self._issued[ident] = decision
            if previous is None and _is_default(decision):
                continue
            out.append(decision)
        return out

    def _single(self, block: _Block) -> list[Decision]:
        member = block.members[0]
        return [SetMode(member.key, "single"), *self._outcome_of(member)]

    def _grouped(self, block: _Block) -> list[Decision]:
        shown = block.shown()
        window = block.window()
        hidden = len(shown) - len(window)
        out: list[Decision] = []
        if shown:
            first = block.members[0].key
            out += [MountHeader(block.key, before=first), self._head(block)]
        out += self._phrase(
            block,
            "top",
            more_phrase(hidden) if hidden else None,
            after=block.key,
        )
        # `└ ` — у маркера окна, иначе у первого видимого члена.
        lead = window[0] if window and not hidden else None
        for member in block.members:
            out += self._member(member, lead=member is lead)
            out.append(SetVisible(member.key, member in window))
        collapse = COLLAPSE_PHRASE if block.expanded else None
        out += self._phrase(
            block,
            "bottom",
            collapse if block.expandable() else None,
            after=block.members[-1].key,
        )
        return out

    def _head(self, block: _Block) -> SetHeader:
        members = block.members
        running = any(m.live for m in members)
        stopped = block.stopped or any(
            m.halted or m.not_started for m in members
        )
        glyph = block_glyph(
            running=running,
            stopped=stopped,
            any_ok=any(m.outcome == "ok" for m in members),
        )
        errors = sum(m.outcome == "error" for m in members)
        refusals = sum(m.outcome == "refused" for m in members)
        return SetHeader(
            block.key,
            header_word(block.kind, live=running and not stopped),
            glyph,
            _failure_parts(errors, refusals),
        )

    def _phrase(
        self,
        block: _Block,
        slot: str,
        text: Optional[str],
        *,
        after: str,
    ) -> list[Decision]:
        """Фраза нужна — смонтировать (переставить) и задать текст; не
        нужна, но уже выдана — скрыть."""
        if text is not None:
            return [
                MountPhrase(block.key, slot, after=after),
                SetPhrase(block.key, slot, text),
            ]
        if ("SetPhrase", block.key, slot) in self._issued:
            return [SetPhrase(block.key, slot, None)]
        return []

    def _member(self, member: _Member, *, lead: bool) -> list[Decision]:
        mode = SetMode(member.key, "member", lead=lead, current=member.live)
        return [mode, *self._outcome_of(member)]

    def _outcome_of(self, member: _Member) -> list[Decision]:
        """Следствие `└` и застывший таймер законченного вызова."""
        out: list[Decision] = []
        if member.not_started:
            out.append(SetConsequence(member.key, NOT_STARTED_REASON, "dim"))
        elif member.outcome in _REASON_LOOK and member.reason:
            look = _REASON_LOOK[member.outcome]
            out.append(SetConsequence(member.key, member.reason, look))
        timer = self._timer(member)
        if timer is not None:
            out.append(timer)
        return out

    def _timer(self, member: _Member) -> Optional[SetTimer]:
        """Таймер — только у исполнявшегося вызова (спека §3.4)."""
        if not member.done or member.not_started:
            return None
        if member.outcome == "refused":
            return None
        executed = member.executed
        if executed is None:
            executed = member.started or member.key in self._timers
        if not executed:
            return None
        return SetTimer(member.key, self._timers.get(member.key), live=False)


def _is_default(decision: Decision) -> bool:
    """Решение совпадает с видом только что смонтированной строки."""
    return isinstance(decision, SetVisible) and decision.visible
