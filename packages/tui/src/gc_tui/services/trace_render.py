"""Общие строители виджетов трейса — переиспользуются главным экраном (через
делегирование из TraceScreen._render_event) и read-only превью.

Чистые: конструируют и возвращают виджеты, не монтируют, не трогают экран/состояние."""

from __future__ import annotations

import json
import logging
import re
import textwrap
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Optional

from gc_core.constants.tool_display import (
    DisplayCategory,
    McpToolInfo,
    tool_display,
)
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.utils.path_truncate import truncate_path

from gc_tui.models import AgentHead, ResultOutcome, TraceEvent
from gc_tui.palette import INK
from gc_tui.services.duration_format import format_round_int, format_timer
from gc_tui.services.trace_glyphs import PROMPT
from gc_tui.services.turn_outcome import kind_of, turn_head

if TYPE_CHECKING:
    from gc_core.tools.turn.turn_close import TurnClose
    from gc_core.vision.notes import TraceNote
    from rich.text import Text

    from gc_tui.widgets.consequence_line import ConsequenceLine
    from gc_tui.widgets.tool_blocks_view import ToolBlocksView
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from gc_tui.widgets.trace_row import (
        CompactionBody,
        CompactionRule,
        ConsequenceTraceRow,
        TraceRow,
    )
    from gc_tui.widgets.turn_line import TurnLine

_log = logging.getLogger(__name__)

# Парсер `[tag] rest` для default meta-кейса (трейс и read-only превью).
TAG_RE = re.compile(r"^\[(?P<tag>[^\]]+)\]\s*(?P<rest>.*)$")

# Уровень вложенности события — единственный источник отступа ленты: глиф
# события уровня n стоит в колонке `●` реплики агента плюс 2n. Живой трейс
# (TraceScreen._mount_nested_*, main.py) и read-only превью
# (узел вложенного трейса `nested_trace`) берут отступ отсюда.
LEVEL_INDENT = "  "


def level_prefix(level: int) -> str:
    """Отступ события уровня ``level`` (0 — верхний уровень, без отступа)."""
    return LEVEL_INDENT * level


# Глифы колонки `●`: реплика агента (итоговая — белым, промежуточная —
# серым по классу `line-intermediate`) и реплика пользователя.
PROSE_BULLET = "● "
USER_PREFIX = PROMPT
NESTED_PROSE_PREFIX = level_prefix(1) + PROSE_BULLET
NESTED_USER_PREFIX = level_prefix(1) + USER_PREFIX
LINE_INTERMEDIATE_CLASS = "line-intermediate"
# Вложенная реплика субагента: глиф `●` серым (CSS по классу).
NESTED_PROSE_CLASS = f"{LINE_INTERMEDIATE_CLASS} nested-prose"
LINE_USER_CLASS = "line-user"
LINE_SUMMARY_CLASS = "line-summary"
LINE_NOTE_CLASS = "line-note"
# Метка записи конца хода жирная и прямая — и в курсивной строке итога.
_RECORD_LABEL_STYLE = "bold not italic"

# Следствия вопроса ask_human — строкой `└` внутри его рамки (спека P3 §3).
QUESTION_UNANSWERED = "без ответа"
QUESTION_INTERRUPTED = "прервано — продолжите сообщением или Esc"


def consequence_lead(level: int) -> Text:
    """Начало строки следствия события уровня ``level``: `└ ` на +2 от
    его глифа."""
    from rich.text import Text

    from gc_tui.palette import DIM

    lead = Text(level_prefix(level + 1))
    lead.append("└ ", style=DIM)
    return lead


def build_consequence_line(
    level: int, body: Text, *, selectable: bool = True
) -> ConsequenceLine:
    """Строка следствия `└ body` события уровня ``level``.

    Общий вид для вызова, реплики и вопроса — следствие рисуется внутри
    виджета своего события (спека P3 §3); перенос тела — под текстом
    после `└ `. ``selectable=False`` — тело вне выделения мышью."""
    from gc_tui.widgets.consequence_line import ConsequenceLine

    return ConsequenceLine(
        consequence_lead(level), body, selectable=selectable
    )


# Строка без владельца (спека P3 §10, ответ 5): глиф `!` в колонке `●`,
# цвет — классом важности; статус вызова — круги `●`/`○`.
STANDALONE_PREFIX = "! "


def build_standalone_row(
    text: str,
    *,
    severity: Literal["error", "warn"],
    event_no: int = 0,
) -> TraceRow:
    """Строка ленты без владельца: `! текст`, `ERR` или `WARN` классом;
    `line-standalone` — пустая строка сверху, как у любого события (§2.1)."""
    from gc_tui.widgets.trace_row import TraceRow

    return TraceRow(
        prefix=STANDALONE_PREFIX,
        content=text,
        kind_class=f"line-{severity} line-standalone",
        event_no=event_no,
    )


def build_orphan_result_row(
    summary: str, outcome: ResultOutcome, *, event_no: int = 0
) -> TraceRow:
    """Итог вызова, к которому нет строки вызова (ушла в сжатие, call_id
    неизвестен). Ошибка — строка `!` (`ERR`); отказ и стоп — не сбой: глиф
    `○` цветом исхода (отказ — `REFUSED`, стоп — `DIM`, таблица исходов
    строки вызова `_OUTCOME_LOOK`) и серый текст «кто: почему» (спека P3
    §10; у строки вызова причина отказа — цветом отказа, P4 §9.13). Один
    билдер для живой ленты и превью."""
    from rich.text import Text

    from gc_tui.palette import DIM
    from gc_tui.widgets.tool_call_row import _OUTCOME_LOOK
    from gc_tui.widgets.trace_row import TraceRow

    if outcome not in ("refused", "halted"):
        return build_standalone_row(
            summary, severity="error", event_no=event_no
        )
    glyph, glyph_style, _line_style = _OUTCOME_LOOK[outcome]
    return TraceRow(
        prefix=Text().append(f"{glyph} ", style=glyph_style),
        content=Text().append(summary, style=DIM),
        kind_class="line-standalone",
        event_no=event_no,
    )


def note_consequence(note: TraceNote) -> Text:
    """Тело следствия-пометки под владельцем по тону пометки: `warn` —
    `! текст` цветом `WARN`, `info` — серый текст `DIM` без `!` (строку `└`
    дорисует владелец — `add_consequence`)."""
    from rich.text import Text

    from gc_tui.palette import DIM, WARN

    prefix, style = {
        "warn": (STANDALONE_PREFIX, WARN),
        "info": ("", DIM),
    }[note.tone]
    body = Text()
    body.append(prefix + note.text, style=style)
    return body


# Лестница подписи метки сжатия (макеты p3_compaction_*): первая, что
# влезла между полосами. Подпись без недостающих полей (сводка вне
# сегментов — без номера, тред без счёта — без чисел) пропускается.
COMPACTION_LABELS = (
    "контекст сжат #{num} · {before} → {after} символов",
    "контекст сжат #{num} · {before} → {after}",
    "сжат #{num} · {before} → {after}",
    "контекст сжат #{num}",
    "сжат #{num}",
    "контекст сжат",
    "сжат",
)
_RULE = "─"
_RULE_MIN = 2  # полоса с каждой стороны не короче


def _compaction_label_values(num: Optional[int], comp: Mapping) -> dict:
    values: dict = {} if num is None else {"num": num}
    before, after = comp.get("chars_before"), comp.get("chars_after")
    if before and after:
        values["before"] = format_round_int(before)
        values["after"] = format_round_int(after)
    return values


def compaction_labels(num: Optional[int], comp: Mapping) -> list[str]:
    """Ступени подписи метки от полной к короткой — по данным сжатия."""
    values = _compaction_label_values(num, comp)
    labels = []
    for template in COMPACTION_LABELS:
        try:
            labels.append(template.format_map(values))
        except KeyError:
            continue
    return labels


def full_width_rule(label: Text, width: int, *, bar_style: str) -> Text:
    """Подпись между полосами во всю ширину ``width``: `──── label ────`.

    Единственный сборщик меток-черт ленты (сжатие контекста, смена
    модели): полосы — ``bar_style``, не короче `_RULE_MIN`; подпись не
    влезла — обрезается с `…`. Пробелы вокруг подписи — её стилем."""
    from rich.text import Text

    middle = label.copy()
    middle.truncate(max(width - 2 * _RULE_MIN - 2, 1), overflow="ellipsis")
    middle.pad(1)
    bars = max(width - middle.cell_len, 2 * _RULE_MIN)
    text = Text()
    text.append(_RULE * (bars // 2), style=bar_style)
    text.append_text(middle)
    text.append(_RULE * (bars - bars // 2), style=bar_style)
    return text


def compaction_rule(
    num: Optional[int], comp: Mapping, width: int, *, expanded: bool
) -> Text:
    """Метка сжатия во всю ширину ``width``: `──── ▸ подпись ────`.

    Полосы — `FRAME_ARCHIVE`, подпись — `DIM` (спека P3 §2.5, §10 B).
    Подпись — первая ступень лестницы, при которой полосы не короче
    `_RULE_MIN`; не влезла и последняя — она обрезается с `…`."""
    from rich.cells import cell_len
    from rich.text import Text

    from gc_tui.palette import DIM, FRAME_ARCHIVE

    arrow = "▾" if expanded else "▸"
    budget = width - 2 * _RULE_MIN - len(f" {arrow}  ")
    labels = compaction_labels(num, comp)
    label = next((x for x in labels if cell_len(x) <= budget), labels[-1])
    return full_width_rule(
        Text(f"{arrow} {label}", style=DIM), width, bar_style=FRAME_ARCHIVE
    )


def _segments(text: str, lead: Optional[Text]) -> list:
    """Сегменты текста; с `lead` первым всегда идёт проза — строка под него."""
    from gc_tui.services.markdown_render import Block, segment_answer

    segments = segment_answer(text)
    if lead is not None and (not segments or segments[0].kind != "prose"):
        segments.insert(0, Block(kind="prose"))
    return segments


def _prose_text(text: str, ink: str, lead: Optional[Text]) -> Text:
    from gc_tui.services.markdown_render import render_prose

    content = render_prose(text, ink=ink)
    return content if lead is None else lead + content


def build_compaction_marker_event(
    no: int, segment_index: int, comp: dict
) -> TraceEvent:
    """Событие-маркер сжатия сегмента ``segment_index`` (его ``compacted_by``).

    Одно на полную перерисовку и шаг сжатия: оба пути строят маркер одинаково.
    Номер и данные сжатия — в ``full_payload``: по ним метка пересобирается
    под ширину ленты."""
    num = segment_index + 1
    return TraceEvent(
        no=no,
        event_ts_utc_iso=comp.get("ts", ""),
        kind="summary",
        title="summary",
        summary=compaction_labels(num, comp)[0],
        summary_content=comp["synthesis"],
        full_payload={"num": num, "comp": comp},
    )


def build_compaction_widgets(
    ev: TraceEvent, *, focusable: bool
) -> tuple[CompactionRule, Optional[CompactionBody]]:
    """Метка сжатия и свёрнутое тело сводки — для живой ленты и превью.

    ``focusable=False`` — превью: Enter там открывает тред, а не сводку.
    Событие сводки вне сегментов (``dialog_summary`` маппера) номера и
    счёта не несёт — метка без чисел. Тела нет без текста сводки."""
    from gc_tui.widgets.trace_row import CompactionBody, CompactionRule

    payload = ev.full_payload if isinstance(ev.full_payload, dict) else {}
    comp = payload.get("comp") or {}
    rule = CompactionRule(
        num=payload.get("num"),
        comp=comp,
        event_no=ev.no,
        focusable=focusable,
    )
    text = (ev.summary_content or "").strip()
    if not text:
        return rule, None
    body = CompactionBody(
        messages=comp.get("messages_in_segment"),
        text=text,
        event_no=ev.no,
    )
    return rule, body


def build_block_widgets(
    text: str,
    *,
    prose_bullet: bool,
    event_no: int,
    bullet: str = PROSE_BULLET,
    kind_class_base: str = "line-agent",
    ink: str = INK,
    lead: Optional[Text] = None,
) -> list:
    """Markdown → виджеты трейса. `prose_bullet` — префикс `bullet` на прозе
    (трейс/превью, по умолчанию `● `); False — без него (баннер ask_human).
    `kind_class_base` — класс прозовых строк (intermediate_reply →
    line-intermediate, глиф серым). Код и таблицы — BlockPanel.
    focusable=True только у первого виджета события.
    `ink` — стиль простого текста прозы (см. render_prose); `lead` — начало
    первой строки (метка записи)."""
    from gc_tui.widgets.block_panel import BlockPanel
    from gc_tui.widgets.trace_row import TraceRow

    widgets: list = []
    focus_set = False
    first_prose_seen = False
    for seg in _segments(text, lead):
        if seg.kind == "code-block":
            widgets.append(
                BlockPanel.for_code(
                    seg.content,
                    seg.lang,
                    event_no=event_no,
                    focusable=not focus_set,
                    indent=prose_bullet,
                )
            )
            focus_set = True
            continue
        if seg.kind == "table":
            widgets.append(
                BlockPanel.for_table(
                    seg.headers or [],
                    seg.rows or [],
                    seg.aligns or [],
                    event_no=event_no,
                    focusable=not focus_set,
                    indent=prose_bullet,
                )
            )
            focus_set = True
            continue
        kind_class = (
            kind_class_base
            if not first_prose_seen
            else f"{kind_class_base} line-agent-cont"
        )
        marker = bullet if not first_prose_seen else "  "
        prefix = marker if prose_bullet else ""
        # Метка — только в первой строке (она проза, см. _segments).
        content = _prose_text(seg.content, ink, lead)
        lead = None
        first_prose_seen = True
        widgets.append(
            TraceRow(
                prefix=prefix,
                content=content,
                kind_class=kind_class,
                event_no=event_no,
                focusable=not focus_set,
            )
        )
        focus_set = True
    return widgets


def build_answered_question_widget(
    question,
    *,
    event_no: int = 0,
    extra_classes: str = "",
    call_id: Optional[str] = None,
):
    """Отвеченный вопрос ask_human как строка истории: серая round-рамка
    (CSS .answered-question) + янтарный маркер '?'. Тех-префикс 'ask_human:'
    не добавляем. Общий для live (main) и readonly (preview) путей.
    Следствия (`└ без ответа`, `└ прервано …`) — `QuestionRow.add_consequence`.

    Цвет рамки — токен $frame-archive (серый). Альтернатива (живое сравнение
    после релиза) — янтарная рамка: сменить border в .answered-question на
    $run-color, точка правки одна."""
    from rich.text import Text

    from gc_tui.palette import DIM, RUN_COLOR
    from gc_tui.widgets.trace_row import QuestionRow

    content = Text()
    content.append("? ", style=RUN_COLOR)
    content.append(question, style=DIM)
    cls = ("answered-question " + extra_classes).strip()
    return QuestionRow(
        call_id=call_id,
        prefix="",
        content=content,
        kind_class=cls,
        event_no=event_no,
        focusable=True,
    )


def note_question(question, note: str) -> None:
    """Следствие вопроса `└ note` серым — один вид для ленты и превью."""
    question.add_consequence(question_note_body(note))


def question_note_body(note: str) -> Text:
    """Тело следствия вопроса (`note_question`)."""
    from rich.text import Text

    from gc_tui.palette import DIM

    return Text(note, style=DIM)


# Промежуточный ответ субагента, который ядро не приняло (решение
# владельца P3b, 3): следствие серым, не ошибка.
NESTED_PROSE_REJECTED = "· не принят"


def build_nested_prose_row(
    prose: str, *, ok: bool = True, error_brief: Optional[str] = None
) -> ConsequenceTraceRow:
    """Промежуточная реплика субагента: серый `●` на уровне 1.

    Не принятая ядром — со следствием `└ · не принят — причина` (`DIM`)
    внутри виджета. Один билдер для живой ленты и превью."""
    from rich.text import Text

    from gc_tui.palette import DIM
    from gc_tui.widgets.trace_row import ConsequenceTraceRow

    consequences = []
    if not ok:
        note = NESTED_PROSE_REJECTED
        if error_brief:
            note = f"{note} — {error_brief}"
        consequences.append(Text(note, style=DIM))
    return ConsequenceTraceRow(
        prefix=NESTED_PROSE_PREFIX,
        content=prose,
        kind_class=NESTED_PROSE_CLASS,
        event_no=0,
        focusable=False,
        consequence_level=1,
        consequences=consequences,
    )


def build_agent_widgets(summary: str, *, event_no: int) -> list:
    """Ответ агента в трейсе/превью: проза с `● `, код панелями, таблицы сеткой."""
    return build_block_widgets(summary, prose_bullet=True, event_no=event_no)


def build_intermediate_widgets(summary: str, *, event_no: int) -> list:
    """Промежуточная реплика intermediate_reply: тот же markdown/код-пайплайн,
    что и проза (build_agent_widgets), но класс line-intermediate — глиф `●`
    серым (спека P3 §10)."""
    return build_block_widgets(
        summary,
        prose_bullet=True,
        event_no=event_no,
        kind_class_base=LINE_INTERMEDIATE_CLASS,
    )


def _record_label(label: str) -> Text:
    from rich.text import Text

    return Text.assemble((f"{label}:", _RECORD_LABEL_STYLE), " ")


def note_text(comment: str) -> Text:
    """Контент строки заметки turn_finish: метка жирная и прямая, текст
    ремарки — курсивом (цвет/отступ берутся из CSS `.line-note`)."""
    from gc_core.tools.turn.turn_close import LABEL_NOTE

    t = _record_label(LABEL_NOTE)
    t.append(comment, style="italic")
    return t


def tool_caption(
    wire_name: str,
    mcp_tools: Mapping[str, McpToolInfo] | None = None,
    mcp_servers: Iterable[str] = (),
    *,
    nested: bool = False,
) -> tuple[DisplayCategory, str]:
    """Категория и имя тула для ленты по отображению ядра (спека P3 §2.1).

    MCP — `сервер · тул`; тул без подписи (неизвестный или скрытый
    политикой) — сырое wire-имя; остальные — подпись ядра. ``nested`` —
    вызов субагента (вложенная таблица ядра)."""
    shown = tool_display(
        wire_name,
        nested=nested,
        mcp_tools=mcp_tools,
        mcp_servers=mcp_servers,
    )
    if shown.category is DisplayCategory.MCP:
        return shown.category, f"{shown.label} · {shown.detail}"
    if shown.category in (DisplayCategory.CALLED, DisplayCategory.HIDDEN):
        return shown.category, wire_name
    return shown.category, shown.label


def _call_caption(
    wire_name: str,
    args_summary: str,
    mcp_tools: Mapping[str, McpToolInfo] | None,
    mcp_servers: Iterable[str],
    nested: bool,
) -> tuple[str, str]:
    """(подпись, объект) строки вызова: тул без подписи (неизвестный или
    скрытый, но всё же дошедший до ленты) — `Called` и сырое имя в
    объекте; память — глагол из действия и цель (`Write  goal`), без
    действия — тоже `Called` (слово `Memory` повторяло бы заголовок)."""
    category, name = tool_caption(
        wire_name, mcp_tools, mcp_servers, nested=nested
    )
    if category in (DisplayCategory.CALLED, DisplayCategory.HIDDEN):
        target = " ".join(p for p in (name, args_summary) if p)
        return DisplayCategory.CALLED.value, target
    if category == DisplayCategory.MEMORY:
        if not args_summary:
            return DisplayCategory.CALLED.value, wire_name
        # Сводка ядра — `действие цель`: глагол члена блока Memory.
        action, _, target = args_summary.partition(" ")
        return action.capitalize(), target
    return name, args_summary


#: Объект строки вызова под длинным хвостом не короче (если сам длиннее):
#: хвост режется после того, как объект дошёл до этого минимума.
CALL_TARGET_MIN_COLS = 16


def fit_call_target(target: str, cols: int, *, path: bool) -> str:
    """Объект строки вызова в `cols` символов: путь — серединой в `…`
    (`truncate_path`), остальное (команда, запрос) — с конца в `…`."""
    if len(target) <= cols:
        return target
    if cols <= 0:
        return ""
    if path:
        return truncate_path(target, cols)
    return target[: cols - 1].rstrip() + "…"


#: Слово строки мысли модели: идёт / после (спека мыслей §2).
THOUGHT_LIVE_WORD = "Thinking"
THOUGHT_DONE_WORD = "Thought"
#: Глиф строки мысли (с пробелом до слова) и разделитель слова и хвоста.
THOUGHT_GLYPH = "● "
_THOUGHT_SEP = "  "


@dataclass(frozen=True)
class ThoughtLine:
    """Части строки мысли под ширину: слово, хвост текста, таймер
    (` · 4с` или пусто); ``cut`` — хвост обрезан, текст раскрываем."""

    word: str
    tail: str
    timer: str
    cut: bool

    @property
    def lead(self) -> str:
        """Слово с разделителем перед хвостом (без хвоста — слово)."""
        return self.word + (_THOUGHT_SEP if self.tail else "")


#: Граница предложения в мысли: после `.`/`!`/`?`/`…` и пробела.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?…])\s+")


def _last_sentence_head(flat: str, room: int) -> str:
    """Последнее предложение с начала в ``room`` знаков: до него был
    текст — `…` в начале; не влезло — обрезка по границе слова, `…` в
    конце."""
    pieces = _SENTENCE_BREAK.split(flat)
    lead = "…" if len(pieces) > 1 else ""
    sentence = lead + pieces[-1]
    if len(sentence) <= room:
        return sentence
    head = sentence[: room - 1]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip() + "…"


def thought_line(
    *,
    live: bool,
    text: str,
    seconds: float | None,
    width: int,
    expanded: bool = False,
) -> ThoughtLine:
    """Строка мысли в ``width`` клеток: глиф, слово и таймер не режутся.
    Не влезло — у идущей хвост с `…` в начале (видны новейшие слова), у
    законченной последнее предложение с начала (`…` в начале, если до
    него был текст, и в конце, если не влезло). Раскрытая
    (``expanded``) — без текста: он весь под строкой. Ширины нет
    (``<= 0``) — текст целиком."""
    from rich.cells import cell_len

    from gc_tui.services.stream_sink import reasoning_tail

    word = THOUGHT_LIVE_WORD if live else THOUGHT_DONE_WORD
    timer = format_timer(seconds) if seconds is not None else None
    timer = f" · {timer}" if timer else ""
    flat = " ".join(text.split())
    room = width - cell_len(THOUGHT_GLYPH + word + _THOUGHT_SEP + timer)
    if width <= 0 or cell_len(flat) <= room:
        return ThoughtLine(word, "" if expanded else flat, timer, cut=False)
    if expanded:
        tail = ""
    elif room <= 1:
        tail = "…"
    elif live:
        # `reasoning_tail` держит `maxlen` знаков после `…`.
        tail = reasoning_tail(flat, room - 1)
    else:
        tail = _last_sentence_head(flat, room)
    return ThoughtLine(word, tail, timer, cut=True)


#: Строк задачи субагента под заголовком `Agent`; дальше — `…`.
AGENT_TASK_LINES = 2


def fit_lines(text: str, cols: int, max_lines: int) -> str:
    """Текст переносом по словам в ``cols`` колонок, не больше
    ``max_lines`` строк (через `\\n`); не влез — конец последней `…`.
    Колонка ещё без ширины — текст одной строкой целиком."""
    flat = " ".join(text.split())
    if cols <= 1:
        return flat
    lines = textwrap.wrap(flat, cols)
    if len(lines) <= max_lines:
        return "\n".join(lines)
    kept = lines[:max_lines]
    kept[-1] = kept[-1][: cols - 1].rstrip() + "…"
    return "\n".join(kept)


def build_call_row(
    *,
    call_id: str,
    wire_name: str,
    args_summary: str,
    level: int,
    event_no: int = 0,
    waiting: bool = False,
    mcp_tools: Mapping[str, McpToolInfo] | None = None,
    mcp_servers: Iterable[str] = (),
    agent: AgentHead | None = None,
    think_text: str = "",
) -> ToolCallRow:
    """Строка вызова — один билдер для живой ленты, превью и вложенных.

    ``agent`` — заголовок вызова субагента: объект строки — его роль,
    задача — строкой `└` под ней."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    label, target = _call_caption(
        wire_name, args_summary, mcp_tools, mcp_servers, level > 0
    )
    if agent is not None:
        target = agent.role
    shown = tool_display(
        wire_name, mcp_tools=mcp_tools, mcp_servers=mcp_servers
    )
    return ToolCallRow(
        call_id=call_id,
        label=label,
        target=target,
        path_target=shown.primary_arg == "path",
        skill_target=shown.primary_arg == "skill_id",
        wire_name=wire_name,
        event_no=event_no,
        waiting=waiting,
        level=level,
        task=agent.task if agent is not None else "",
        think_text=think_text,
    )


def build_summary_widgets(note: str, *, event_no: int) -> list:
    """Запись «* итог: …» после ответа модели: тот же Markdown-пайплайн, что
    у ответа (списки, блоки кода), метка жирная; цвет и курсив текста — от
    CSS `.line-summary`, не от разметки."""
    from gc_core.tools.turn.turn_close import LABEL_SUMMARY

    return build_block_widgets(
        note,
        prose_bullet=True,
        event_no=event_no,
        bullet="* ",
        kind_class_base=LINE_SUMMARY_CLASS,
        ink="",
        lead=_record_label(LABEL_SUMMARY),
    )


def _summary_comment_row(comment: str, event_no: int):
    """Оговорка итога — отдельная строка-продолжение записи, без пустой
    строки сверху: после списка, кода и таблицы она не сливается с блоком."""
    from gc_tui.services.markdown_render import render_prose
    from gc_tui.widgets.trace_row import TraceRow

    return TraceRow(
        prefix="  ",
        content=render_prose(comment, ink=""),
        kind_class=f"{LINE_SUMMARY_CLASS} line-agent-cont",
        event_no=event_no,
        focusable=False,
    )


def finish_close_of(ev) -> TurnClose:
    """Конец хода по событию turn_finish (sidecar-ключи маппера)."""
    from gc_core.tools.turn.submit_solution import TurnFinishView
    from gc_core.tools.turn.turn_close import finish_close

    payload = ev.full_payload if isinstance(ev.full_payload, dict) else {}
    view = TurnFinishView(ev.summary, payload.get("_submit_comment", ""))
    return finish_close(view, payload.get("_submit_reply", ""))


def build_finish_widgets(close: TurnClose, *, event_no: int) -> list:
    """Виджеты конца хода: отчёт ответом (если он не ушёл в итог), запись
    «итог» или строка «заметка»."""
    from gc_core.tools.turn.turn_close import LABEL_NOTE, LABEL_SUMMARY

    from gc_tui.widgets.trace_row import TraceRow

    if close.label == LABEL_SUMMARY:
        widgets = build_summary_widgets(close.note, event_no=event_no)
        if close.comment != "":
            widgets.append(_summary_comment_row(close.comment, event_no))
        return widgets
    widgets = build_agent_widgets(close.body, event_no=event_no)
    if close.label == LABEL_NOTE:
        widgets.append(
            TraceRow(
                prefix="* ",
                content=note_text(close.comment),
                kind_class=LINE_NOTE_CLASS,
                event_no=event_no,
                focusable=False,
            )
        )
    return widgets


def build_turn_line(
    outcome: TurnOutcome | None = None,
    *,
    word: str | None = None,
    plan: bool = False,
    opened_by: str | None = None,
    now: datetime | None = None,
) -> TurnLine:
    """Строка итога хода — одна для ленты и превью: голова `turn_head`
    без сводки правок и причины.

    Источник ровно один: запись итога (`outcome`) или исход без записи
    (`word` и `plan`: запасной исход раннера, статус сессии) — у такой
    головы нет ни длительности, ни времени конца."""
    from gc_tui.widgets.turn_line import TurnLine

    if (outcome is None) == (word is None):
        raise ValueError("нужна запись итога или слово исхода, ровно одно")
    if outcome is None:
        head = turn_head(
            word, plan=plan, duration_sec=None, ended_at=None, now=now
        )
        return TurnLine(
            head, opened_by=opened_by, word=word, plan=plan, recorded=False
        )
    word, plan = kind_of(outcome)
    head = turn_head(
        word,
        plan=plan,
        duration_sec=outcome.duration_sec,
        ended_at=outcome.ended_at,
        now=now,
    )
    return TurnLine(
        head,
        opened_by=outcome.opened_by,
        word=word,
        plan=plan,
        recorded=True,
    )


def raw_output(ev) -> Optional[str]:
    """Поле `output` из full_payload result-event как raw string."""
    if isinstance(ev.full_payload, dict):
        val = ev.full_payload.get("output")
        if isinstance(val, str):
            return val
        if val is not None:
            # На случай если backend вернул object вместо строки.
            return json.dumps(val, ensure_ascii=False)
    return None


def result_with_edits(
    view: ToolBlocksView,
    row,
    ev,
    *,
    edits_lookup,
    window: int,
    body: bool = False,
) -> None:
    """Исход вызова истории — через модель уровня, с правкой из журнала
    треда тем же билдером, что вживую; сбой журнала — без правки, причина
    — в лог. ``body`` — дифф телом и вне окна раскрытых (ход вернулся
    таким, каким был снят)."""
    from gc_tui.services.block_edits import call_edit, ran_result_of
    from gc_tui.services.tool_blocks import BlockKind

    try:
        edit, compact = call_edit(
            edits_lookup, ev.call_id or "", window=window
        )
        ran = None
        if view.model.kind_of(row.call_id) == BlockKind.RAN:
            ran = ran_result_of(edits_lookup, ev, window=window)
    except Exception as exc:
        _log.warning("правки вызова %s не прочитаны: %r", ev.call_id, exc)
        edit, compact, ran = None, False, None
    view.result(row, ev, edit, compact=compact and not body, ran=ran)
