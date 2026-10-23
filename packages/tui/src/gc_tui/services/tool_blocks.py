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
from typing import Literal, Mapping, Optional, Sequence, Union

from gc_core.agent_edits.journal import EditRecord
from gc_core.config_modules.agent_edits import DEFAULT_TRACE_INLINE_MAX_LINES
from gc_core.constants.tool_display import DisplayCategory

from gc_tui.models import ResultOutcome
from gc_tui.services.duration_format import format_size
from gc_tui.services.plural import (
    errors_word,
    files_word,
    lines_word,
    refusals_word,
)

#: Видимых элементов в окне блока: членов, файлов, событий субагента.
WINDOW = 3
#: Строк превью вывода команды.
OUTPUT_PREVIEW_LINES = 3
#: Строк команды `Ran` в свёрнутом виде: первая в строке и продолжение.
COMMAND_PREVIEW_LINES = 3
#: Экранных рядов диффа на файл в блоке правок — не меньше.
MIN_DIFF_ROWS_PER_FILE = 3

COLLAPSE_PHRASE = "− свернуть"
#: Ключ окна субагента в модели его уровня (спека §7): фраза окна
#: `└ … ещё N` и строка `Agent` у исполнителя.
AGENT_WINDOW = "agent"
NOT_STARTED_REASON = "не запущено (стоп)"
#: Причина идущего члена, оборванного остановкой хода.
CANCELLED_REASON = "отменено"

#: Вид глифа заголовка блока.
GlyphLook = Literal["run", "ok", "error", "halted"]
#: Вид цвета куска текста; токен палитры назначает исполнитель.
Look = Literal[
    "run", "ok", "error", "refused", "halted", "dim", "ink", "muted"
]
RowMode = Literal["single", "header", "member"]
#: Колонка диффа: одиночная правка (2) или член блока (4).
DiffColumn = Literal["single", "member"]
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


def edit_verb(op: Optional[str], *, failed: bool) -> str:
    """Глагол правки тула по результату (§6.1): сбой или отказ —
    `Edit`; создан файл — `Added`; иначе `Edited`. Идущая — `Editing`
    (`header_word`)."""
    if failed:
        return "Edit"
    return "Added" if op == "create" else "Edited"


def command_file_verb(op: Optional[str]) -> str:
    """Глагол файла под командой: `Added` / `Deleted` / `Edited`."""
    return {"create": "Added", "delete": "Deleted"}.get(op or "", "Edited")


def diff_rows(n_applied: int, preview: int) -> int:
    """Экранных рядов диффа на файл (§6.3): одиночная правка — `preview`,
    блок из `n` файлов — `max(3, preview // n)`."""
    if n_applied <= 1:
        return preview
    return max(MIN_DIFF_ROWS_PER_FILE, preview // n_applied)


def counts_text(add: int, rem: int) -> Parts:
    """Счётчики правки ` (+a −r)`; нулевая часть опускается, обе
    нулевые — пусто."""
    nums: list[tuple[str, Look]] = []
    if add:
        nums.append((f"+{add}", "ok"))
    if rem:
        nums.append((f"−{rem}", "error"))
    if not nums:
        return ()
    parts: list[tuple[str, Look]] = [(" (", "dim"), nums[0]]
    if len(nums) == 2:
        parts += [(" ", "dim"), nums[1]]
    parts.append((")", "dim"))
    return tuple(parts)


def diff_key(key: str) -> str:
    """Ключ виджета диффа строки (файла) ``key``."""
    return f"{key}:diff"


def phrase_key(block: str, slot: str) -> str:
    """Ключ виджета фразы ``slot`` блока ``block``."""
    return f"{block}:{slot}"


def block_glyph(*, running: bool, stopped: bool, any_ok: bool) -> GlyphLook:
    """Глиф заголовка (§3.3): стоп — `halted`; идёт — `run`; закончился —
    `ok`, если хоть один вызов прошёл, иначе `error`."""
    if stopped:
        return "halted"
    if running:
        return "run"
    return "ok" if any_ok else "error"


@dataclass(frozen=True)
class BlockEdit:
    """Что модель знает о правке файла: из записи журнала правок."""

    key: str
    path: str
    verb: str
    add: int
    rem: int
    #: Логических строк диффа; 0 — диффа нет (бинарь, удаление).
    diff_lines: int
    size_bytes: Optional[int]
    has_record: bool
    #: Что кладёт в буфер ❐ строки файла; пусто — кнопки нет.
    copy_text: str = ""
    #: Запись журнала — для `DiffBlock` исполнителя; модель её не читает.
    record: Optional[EditRecord] = field(
        default=None, compare=False, repr=False
    )

    @property
    def has_diff(self) -> bool:
        return self.has_record and self.diff_lines > 0


def _edit_tail(edit: Optional[BlockEdit]) -> Parts:
    """Хвост строки файла: ` (+a −r)`, у бинаря — ` · 12 КБ`."""
    if edit is None or not edit.has_record or edit.verb == "Deleted":
        return ()
    if edit.diff_lines == 0 and edit.size_bytes is not None:
        return ((" · ", "dim"), (format_size(edit.size_bytes), "dim"))
    return counts_text(edit.add, edit.rem)


def _lines(edit: Optional[BlockEdit]) -> int:
    return edit.diff_lines if edit is not None and edit.has_diff else 0


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
    элемент блока); ``current`` — член идёт (`INK`); ``gap`` — пустая
    строка сверху (между файлами блока правок)."""

    key: str
    mode: RowMode
    lead: bool = False
    current: bool = False
    gap: bool = False

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
    """Застывший таймер законченного вызова: ``seconds`` из записи хода
    или, если `None`, часы строки на конце исполнения."""

    key: str
    seconds: Optional[float]

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


#: Роль слова строки: глагол правки, слово команды или `Agent`.
WordRole = Literal["edit", "command", "agent"]


@dataclass(frozen=True)
class SetWord:
    """Слово одиночной строки вместо подписи категории: глагол правки
    (`edit`: `Editing` / `Added` / `Edited` / `Edit`) или слово команды
    (`command`: `Running` / `Ran`); вид слова и объекта строка берёт из
    роли."""

    key: str
    word: str
    role: WordRole = "edit"

    @property
    def ident(self) -> tuple:
        return ("SetWord", self.key)


@dataclass(frozen=True)
class SetEditTail:
    """Хвост строки файла после пути: счётчики или размер бинаря."""

    key: str
    parts: Parts

    @property
    def ident(self) -> tuple:
        return ("SetEditTail", self.key)


@dataclass(frozen=True)
class SetDiffShare:
    """Дифф файла: ``rows`` экранных рядов превью (`None` — целиком) в
    колонке ``column``. Смонтировать при первом решении."""

    key: str
    rows: Optional[int]
    column: DiffColumn

    @property
    def ident(self) -> tuple:
        return ("SetDiffShare", self.key)


@dataclass(frozen=True)
class SetExpanded:
    """Блок `Ran` раскрыт: вся команда и весь вывод."""

    block: str
    expanded: bool

    @property
    def ident(self) -> tuple:
        return ("SetExpanded", self.block)


Decision = Union[
    MountHeader,
    MountPhrase,
    SetMode,
    SetVisible,
    SetHeader,
    SetPhrase,
    SetTimer,
    SetConsequence,
    SetWord,
    SetEditTail,
    SetDiffShare,
    SetExpanded,
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
    #: Место строки на уровне по порядку монтирования.
    pos: int = 0
    started: bool = False
    outcome: Optional[ResultOutcome] = None
    reason: str = ""
    executed: Optional[bool] = None
    # Без результата: процесс оборвался или стоп до старта.
    interrupted: bool = False
    not_started: bool = False
    edit: Optional[BlockEdit] = None

    @property
    def applied(self) -> Optional[BlockEdit]:
        """Правка вызова, если он прошёл."""
        return self.edit if self.outcome == "ok" else None

    @property
    def verb(self) -> str:
        return self.edit.verb if self.edit is not None else "Edited"

    @property
    def done(self) -> bool:
        return self.outcome is not None or self.interrupted

    @property
    def live(self) -> bool:
        return not (self.done or self.waiting)

    @property
    def stopped(self) -> bool:
        """Оборван остановкой хода: идёт, без исхода или с исходом стопа
        (`отменено` / `не запущено (стоп)`); остановка лимитом — нет."""
        return (
            not self.done
            or self.interrupted
            or self.not_started
            or (self.outcome == "halted" and self.reason == CANCELLED_REASON)
        )


@dataclass
class _Block:
    key: str
    kind: BlockKind
    members: list[_Member] = field(default_factory=list)
    expanded: bool = False
    stopped: bool = False
    # Правки: строка ещё идёт и в группу не встала; глагол группы
    # (`None` — в группе одни сбои).
    pending: bool = False
    verb: Optional[str] = None
    parts: Optional[_RanParts] = None

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


@dataclass(frozen=True)
class _RanParts:
    command_hidden: int
    output_hidden: int
    files: tuple[BlockEdit, ...]
    unrecorded: int


@dataclass(frozen=True)
class _File:
    """Файл блока для окна и доли: строк диффа и применился ли (входит
    в `n` формулы §6.3; упавшие и `Deleted` — нет)."""

    key: str
    lines: int
    applied: bool


@dataclass(frozen=True)
class _Layout:
    """Окно файлов блока и счёт скрытого."""

    window: tuple[str, ...]
    #: Рядов диффа на видимый файл; `None` — раскрыт, целиком.
    rows: Optional[int]
    #: Скрытых файлов над окном сейчас (`… ещё M`).
    more: int
    #: Скрытых логических строк в свёрнутом виде (`+ ещё N строк`).
    lines: int
    expandable: bool


def _accepts(group: Optional[_Block], member: _Member) -> bool:
    """Встаёт ли законченная правка в группу над ней (решение плана 4):
    сбой — в любую группу правок; успешная — в группу своего глагола с
    хотя бы одной применившейся."""
    if group is None or group.kind != BlockKind.EDITS or group.pending:
        return False
    if member.outcome != "ok":
        return True
    return group.verb == member.verb


class ToolBlocks:
    """Модель блоков одного уровня ленты: верхнего или субагента.

    ``diff_preview`` — логических строк превью диффа одиночной правки
    (`agent_edits.trace_inline_max_lines`). ``agent_window`` — уровень
    субагента: окно последних `WINDOW` событий (блок — одно событие), над
    ним `└ … ещё N`; строка видна, если видна и в своём блоке, и в окне
    (§7)."""

    def __init__(
        self,
        timers: CallTimers,
        *,
        diff_preview: int = DEFAULT_TRACE_INLINE_MAX_LINES,
        agent_window: bool = False,
    ) -> None:
        self._timers = timers
        self._preview = diff_preview
        self._count = 0
        self._blocks: list[_Block] = []
        self._block_of: dict[str, _Block] = {}
        self._members: dict[str, _Member] = {}
        # Строки уровня по порядку монтирования; `None` — закрывающее
        # событие ленты. Группу строки решает то, что стоит над ней.
        self._order: list[Optional[_Member]] = []
        # Первый блок текущего хода: остановка хода трогает только его.
        self._turn_from = 0
        self._issued: dict[tuple, Decision] = {}
        # Окно субагента: события уровня по порядку (ключ блока или
        # события ленты) и выданная им видимость.
        self._window = (
            _Block(AGENT_WINDOW, BlockKind.AGENT) if agent_window else None
        )
        self._events: list[str] = []
        self._event_shown: dict[str, bool] = {}

    # --- события ---------------------------------------------------------

    def member_added(
        self, call_id: str, category: DisplayCategory, *, waiting: bool
    ) -> list[Decision]:
        """Строка вызова смонтирована: в группу своего вида, если она
        стоит прямо над строкой, иначе в новый блок."""
        kind = block_kind(category)
        if kind is None:
            return []
        member = _Member(call_id, waiting=waiting, pos=len(self._order))
        above = self._block_above(member)
        self._order.append(member)
        self._members[call_id] = member
        if kind in _GROUPED and above is not None and above.kind == kind:
            block = above
        else:
            block = self._new_block(kind)
            # Правка встаёт в группу по результату (§6.4).
            block.pending = kind == BlockKind.EDITS
        block.members.append(member)
        self._block_of[call_id] = block
        return self._render(block) + self._window_changes()

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
        edit: Optional[BlockEdit] = None,
    ) -> list[Decision]:
        """Исход вызова. ``executed`` — исполнялся ли; `None` — решают
        старт строки и реестр таймеров (после переоткрытия). ``edit`` —
        запись журнала правки вызова: приходит вместе с исходом, её
        глагол решает, в какую группу правка встанет."""
        member = self._members.get(call_id)
        if member is None:
            return []
        member.edit = edit
        member.outcome = outcome
        member.reason = reason
        member.executed = executed
        member.waiting = False
        # Исход стопа «не запущено» — вызов не исполнялся, как и не
        # начатый при переводе стопом.
        member.not_started = (
            outcome == "halted" and reason == NOT_STARTED_REASON
        )
        return self._settle(member)

    def ran_parts(
        self,
        call_id: str,
        *,
        command_hidden: int,
        output_hidden: int,
        files: Sequence[BlockEdit],
        unrecorded: int,
    ) -> list[Decision]:
        """Части блока `Ran` (§4.0, §4.2): скрытые строки команды и
        вывода, правки команды и число её незаписанных файлов."""
        block = self._block_of.get(call_id)
        if block is None or block.kind != BlockKind.RAN:
            return []
        block.parts = _RanParts(
            command_hidden, output_hidden, tuple(files), unrecorded
        )
        return self._render(block)

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
        return self._settle(member)

    def turn_stopped(self) -> list[Decision]:
        """Ход остановлен пользователем: блоки хода, где член ещё идёт,
        оборвался или получил исход стопа, — `halted`, в том числе уже
        закрытые; законченные блоки и член, остановленный лимитом, глиф
        не меняют."""
        out: list[Decision] = []
        for block in self._blocks[self._turn_from :]:
            if any(m.stopped for m in block.members):
                block.stopped = True
                out += self._render(block)
        return out

    def turn_ended(self) -> list[Decision]:
        """Запись итога хода: следующие блоки — уже другого хода."""
        self._turn_from = len(self._blocks)
        return []

    def paused(self, call_id: str) -> list[Decision]:
        """Ожидание человека: пауза — часы самой строки, блок не меняется."""
        return []

    def resumed(self, call_id: str) -> list[Decision]:
        return []

    def event_closing(self) -> list[Decision]:
        """Событие ленты не из группы закрыло открытую группу."""
        if self._order and self._order[-1] is not None:
            self._order.append(None)
        return []

    def event_added(self, key: str) -> list[Decision]:
        """Событие уровня не из блоков (проза, чек-лист, реплика) с ключом
        ``key``: закрывает группу; у субагента — событие окна."""
        out = self.event_closing()
        if self._window is None:
            return out
        self._events.append(key)
        return out + self._window_changes()

    def queued_admitted(self) -> list[Decision]:
        """Реплика из очереди впущена ядром — закрывает группу."""
        return self.event_closing()

    def toggle(self, key: str) -> list[Decision]:
        """Раскрыть / свернуть блок по ключу блока, фразы или члена (или
        окно субагента); нечего скрывать — решений нет."""
        if self._is_window(key):
            return self._toggle_window()
        block = self._resolve(key)
        if block is None or not self._expandable(block):
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

    # --- запросы исполнителя ---------------------------------------------

    def kind_of(self, key: str) -> Optional[BlockKind]:
        """Вид блока строки ``key``."""
        block = self._resolve(key)
        return block.kind if block is not None else None

    def block_of(self, key: str) -> Optional[str]:
        """Ключ блока по ключу блока, его фразы или строки."""
        if self._is_window(key):
            return AGENT_WINDOW
        block = self._resolve(key)
        return block.key if block is not None else None

    def first_call(self, block: str) -> Optional[str]:
        """Первый член блока — верх блока без заголовка."""
        found = self._block_of.get(block)
        if found is None or not found.members:
            return None
        return found.members[0].key

    def last_call(self, block: str) -> Optional[str]:
        """Последний член блока — его экран открывает Enter на заголовке."""
        found = self._block_of.get(block)
        if found is None or not found.members:
            return None
        return found.members[-1].key

    # --- отрисовка -------------------------------------------------------

    def _resolve(self, key: str) -> Optional[_Block]:
        """Блок ключа строки, блока или фразы (`<блок>:<место>`)."""
        return self._block_of.get(key) or self._block_of.get(
            key.rsplit(":", 1)[0]
        )

    def _new_block(self, kind: BlockKind) -> _Block:
        block = _Block(f"block:{self._count}", kind)
        self._count += 1
        self._blocks.append(block)
        self._block_of[block.key] = block
        if self._window is not None:
            self._events.append(block.key)
        return block

    # --- окно субагента ----------------------------------------------------

    def _is_window(self, key: str) -> bool:
        return self._window is not None and (
            key.split(":", 1)[0] == AGENT_WINDOW
        )

    def _toggle_window(self) -> list[Decision]:
        assert self._window is not None
        if len(self._events) <= WINDOW:
            return []
        self._window.expanded = not self._window.expanded
        return self._window_changes()

    def _shown_events(self) -> list[str]:
        assert self._window is not None
        if self._window.expanded:
            return self._events
        return self._events[-WINDOW:]

    def _shown(self, key: str) -> bool:
        return self._window is None or key in self._shown_events()

    def _window_changes(self) -> list[Decision]:
        """Окно сдвинулось или раскрыто: события, сменившие видимость, —
        заново; фраза окна над событиями."""
        if self._window is None:
            return []
        shown = set(self._shown_events())
        out: list[Decision] = []
        for key in self._events:
            visible = key in shown
            if self._event_shown.get(key, True) == visible:
                continue
            self._event_shown[key] = visible
            block = self._block_of.get(key)
            if block is not None:
                out += self._render(block)
            else:
                out += self._diff([SetVisible(key, visible)])
        hidden = len(self._events) - len(shown)
        text = more_phrase(hidden) if hidden else None
        if self._window.expanded and len(self._events) > WINDOW:
            text = COLLAPSE_PHRASE
        decisions = self._phrase(self._window, "top", text, after=AGENT_WINDOW)
        return out + self._diff(decisions)

    def _windowed(
        self, block: _Block, wanted: list[Decision]
    ) -> list[Decision]:
        """Видимость в окне субагента: блок вне окна скрыт целиком —
        заголовок, строки, файлы и фразы (конъюнкция с окном блока)."""
        if self._window is None:
            return wanted
        shown = self._shown(block.key)
        keys = [m.key for m in block.members]
        if block.parts is not None:
            keys += [f.key for f in block.parts.files]
        out: list[Decision] = []
        covered: set[str] = set()
        for decision in wanted:
            if isinstance(decision, SetVisible):
                covered.add(decision.key)
                if not shown:
                    decision = SetVisible(decision.key, False)
            elif isinstance(decision, SetPhrase) and not shown:
                decision = SetPhrase(decision.block, decision.slot, None)
            out.append(decision)
        out += [SetVisible(k, shown) for k in keys if k not in covered]
        out.append(SetVisible(block.key, shown))
        return out

    def _block_above(self, member: _Member) -> Optional[_Block]:
        """Блок строки, стоящей прямо над ``member``; над ней закрывающее
        событие или ничего — `None`."""
        above = self._order[member.pos - 1] if member.pos else None
        return self._block_of[above.key] if above is not None else None

    def _settle(self, member: _Member) -> list[Decision]:
        """Законченная правка встаёт в группу правок прямо над ней или
        открывает свою; строка над ней ещё не встала — ждёт её. Встав,
        тянет за собой ждавшие правки под ней. У прочих вызовов блок не
        меняется."""
        out: list[Decision] = []
        current: Optional[_Member] = member
        while current is not None and current.done:
            block = self._block_of[current.key]
            above = self._block_above(current)
            if not block.pending or (above is not None and above.pending):
                out += self._render(block)
                break
            out += self._place(current, block, above)
            nxt = current.pos + 1
            current = self._order[nxt] if nxt < len(self._order) else None
        return out

    def _place(
        self, member: _Member, block: _Block, above: Optional[_Block]
    ) -> list[Decision]:
        """Правка ``member`` из своего ждущего блока — в группу ``above``
        (фразы ждущего блока прячутся) или в свой блок."""
        if above is None or not _accepts(above, member):
            block.pending = False
            block.verb = member.verb if member.outcome == "ok" else None
            return self._render(block)
        out = self._phrase(block, "top", None)
        out += self._phrase(block, "bottom", None)
        self._blocks.remove(block)
        del self._block_of[block.key]
        if block.key in self._events:
            self._events.remove(block.key)
            self._event_shown.pop(block.key, None)
        above.members.append(member)
        self._block_of[member.key] = above
        return self._diff(out) + self._render(above) + self._window_changes()

    def _render(self, block: _Block) -> list[Decision]:
        if block.headed:
            wanted = self._grouped(block)
        elif block.kind == BlockKind.EDITS:
            wanted = self._edits(block)
        elif block.kind == BlockKind.RAN:
            wanted = self._ran(block)
        else:
            wanted = self._single(block)
        return self._diff(self._windowed(block, wanted))

    def _expandable(self, block: _Block) -> bool:
        if block.headed:
            return block.expandable()
        if block.kind == BlockKind.EDITS:
            return self._edits_layout(block).expandable
        if block.kind == BlockKind.RAN and block.parts is not None:
            return self._ran_layout(block).expandable
        return False

    def _layout(
        self,
        files: Sequence[_File],
        *,
        expanded: bool,
        extra_lines: int = 0,
        extra_more: int = 0,
    ) -> _Layout:
        """Окно из последних `WINDOW` файлов, доля диффа (§6.3) — по
        применившимся, бинарь в их числе — и счёт скрытого: строки видимых
        сверх доли и все строки скрытых."""
        rows = diff_rows(sum(f.applied for f in files), self._preview)
        tail = files[-WINDOW:]
        in_tail = {f.key for f in tail}
        lines = extra_lines + sum(max(0, f.lines - rows) for f in tail)
        lines += sum(f.lines for f in files if f.key not in in_tail)
        more = len(files) - len(tail) + extra_more
        expandable = bool(lines or more)
        if expanded and expandable:
            keys = tuple(f.key for f in files)
            return _Layout(keys, None, 0, lines, True)
        keys = tuple(f.key for f in tail)
        return _Layout(keys, rows, more, lines, expandable)

    def _edits_layout(self, block: _Block) -> _Layout:
        files = [
            _File(m.key, _lines(m.applied), m.outcome == "ok")
            for m in block.members
        ]
        return self._layout(files, expanded=block.expanded)

    def _ran_layout(self, block: _Block) -> _Layout:
        parts = block.parts
        assert parts is not None
        return self._layout(
            [
                _File(f.key, _lines(f), f.verb != "Deleted")
                for f in parts.files
            ],
            expanded=block.expanded,
            extra_lines=parts.command_hidden + parts.output_hidden,
            extra_more=parts.unrecorded,
        )

    def _bottom(self, block: _Block, layout: _Layout) -> Optional[str]:
        """Единственная фраза раскрытия внизу блока."""
        if not layout.expandable:
            return None
        if block.expanded:
            return COLLAPSE_PHRASE
        return more_lines_phrase(layout.lines) if layout.lines else None

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
        out: list[Decision] = [SetMode(member.key, "single")]
        if block.kind == BlockKind.AGENT:
            word = header_word(BlockKind.AGENT, live=False)
            out.append(SetWord(member.key, word, role="agent"))
        return out + self._outcome_of(member)

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

    def _edits(self, block: _Block) -> list[Decision]:
        """Одиночная правка или блок `Added N` / `Edited N` (§6.2)."""
        members = block.members
        single = len(members) == 1
        layout = self._edits_layout(block)
        out: list[Decision] = []
        if not single:
            n = len(members)
            prefix: Parts = ((f" {n} {files_word(n)}", "ink"),)
            added = [m.applied for m in members if m.applied is not None]
            prefix += counts_text(
                sum(e.add for e in added), sum(e.rem for e in added)
            )
            first = members[0].key
            out += [
                MountHeader(block.key, before=first),
                self._head(block, prefix),
            ]
        more = more_phrase(layout.more) if layout.more else None
        out += self._phrase(block, "top", more, after=block.key)
        for member in members:
            out += self._edit_row(member, layout, single=single)
        last = members[-1]
        out += self._phrase(
            block,
            "bottom",
            self._bottom(block, layout),
            after=_after_file(last.key, last.applied),
        )
        return out

    def _edit_row(
        self, member: _Member, layout: _Layout, *, single: bool
    ) -> list[Decision]:
        if single:
            out: list[Decision] = [
                SetMode(member.key, "single"),
                SetWord(member.key, _edit_word(member)),
            ]
        else:
            # Член блока правок — путь без глагола: `└ путь (+a −r)`.
            out = [_file_mode(member.key, layout), SetWord(member.key, "")]
        visible = member.key in layout.window
        out.append(SetVisible(member.key, visible))
        column: DiffColumn = "single" if single else "member"
        out += _file_parts(member.key, member.applied, layout, column)
        return out + self._outcome_of(member)

    def _ran(self, block: _Block) -> list[Decision]:
        """Строка `Running` / `Ran` и её части: окно правок команды, одна
        фраза внизу блока (§4.0, §4.2)."""
        member = block.members[0]
        word = header_word(BlockKind.RAN, live=not member.done)
        out = [*self._single(block), SetWord(member.key, word, role="command")]
        parts = block.parts
        if parts is None:
            return out
        layout = self._ran_layout(block)
        key = block.members[0].key
        files = parts.files
        more = more_phrase(layout.more) if layout.more else None
        if files:
            out += self._phrase(block, "files", more, before=files[0].key)
        else:
            out += self._phrase(block, "files", more, after=key)
        for edit in files:
            out.append(_file_mode(edit.key, layout, lead=False))
            out.append(SetVisible(edit.key, edit.key in layout.window))
            out += _file_parts(edit.key, edit, layout, "member")
        last = _after_file(files[-1].key, files[-1]) if files else key
        k = parts.unrecorded
        unrecorded = None
        if block.expanded and k:
            unrecorded = f"ещё {k} {files_word(k)} не записано"
        out += self._phrase(block, "unrecorded", unrecorded, after=last)
        if unrecorded is not None:
            last = phrase_key(block.key, "unrecorded")
        bottom = self._bottom(block, layout)
        out += self._phrase(block, "bottom", bottom, after=last)
        out.append(SetExpanded(block.key, block.expanded))
        return out

    def _head(self, block: _Block, prefix: Parts = ()) -> SetHeader:
        members = block.members
        running = any(m.live for m in members)
        # `○` — остановка хода или член без исхода; исход «остановлен»
        # (лимит) у члена сам `○` не ставит.
        stopped = block.stopped or any(m.interrupted for m in members)
        glyph = block_glyph(
            running=running,
            stopped=stopped,
            any_ok=any(m.outcome == "ok" for m in members),
        )
        errors = sum(m.outcome == "error" for m in members)
        refusals = sum(m.outcome == "refused" for m in members)
        live = running and not stopped
        return SetHeader(
            block.key,
            header_word(block.kind, live=live, verb=block.verb),
            glyph,
            prefix + _failure_parts(errors, refusals),
        )

    def _phrase(
        self,
        block: _Block,
        slot: str,
        text: Optional[str],
        *,
        after: Optional[str] = None,
        before: Optional[str] = None,
    ) -> list[Decision]:
        """Фраза нужна — смонтировать (переставить) и задать текст; не
        нужна, но уже выдана — скрыть."""
        if text is not None:
            return [
                MountPhrase(block.key, slot, after=after, before=before),
                SetPhrase(block.key, slot, text),
            ]
        if ("SetPhrase", block.key, slot) in self._issued:
            return [SetPhrase(block.key, slot, None)]
        return []

    def _member(self, member: _Member, *, lead: bool) -> list[Decision]:
        mode = SetMode(member.key, "member", lead=lead, current=member.live)
        block = self._block_of[member.key]
        if block.stopped and member.interrupted and not member.not_started:
            reason = SetConsequence(member.key, CANCELLED_REASON, "dim")
            return [mode, reason, *self._outcome_of(member)]
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
        return SetTimer(member.key, self._timers.get(member.key))


def _edit_word(member: _Member) -> str:
    if not member.done:
        return header_word(BlockKind.EDITS, live=True)
    if member.outcome != "ok":
        return edit_verb(None, failed=True)
    return member.verb


def _file_mode(key: str, layout: _Layout, *, lead: bool = True) -> SetMode:
    """Файл блока: `└ ` у первого видимого без маркера окна (у файлов
    `Ran` его нет — `└` там у вывода); пустая строка — между видимыми
    файлами, не перед первым."""
    first = layout.window[0] if layout.window else None
    visible = key in layout.window
    return SetMode(
        key,
        "member",
        lead=lead and key == first and not layout.more,
        gap=visible and key != first,
    )


def _file_parts(
    key: str,
    edit: Optional[BlockEdit],
    layout: _Layout,
    column: DiffColumn,
) -> list[Decision]:
    """Хвост файла и доля диффа видимому файлу; скрытому дифф не
    монтируется до первого раскрытия."""
    out: list[Decision] = [SetEditTail(key, _edit_tail(edit))]
    if edit is not None and edit.has_diff and key in layout.window:
        out.append(SetDiffShare(key, layout.rows, column))
    return out


def _after_file(key: str, edit: Optional[BlockEdit]) -> str:
    """Последний виджет файла: его дифф или сама строка."""
    if edit is not None and edit.has_diff:
        return diff_key(key)
    return key


def _is_default(decision: Decision) -> bool:
    """Решение совпадает с видом только что смонтированной строки."""
    if isinstance(decision, SetVisible):
        return decision.visible
    if isinstance(decision, SetEditTail):
        return not decision.parts
    if isinstance(decision, SetExpanded):
        return not decision.expanded
    return False
