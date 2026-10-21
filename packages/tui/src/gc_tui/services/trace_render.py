"""Общие строители виджетов трейса — переиспользуются главным экраном (через
делегирование из TraceScreen._render_event) и read-only превью.

Чистые: конструируют и возвращают виджеты, не монтируют, не трогают экран/состояние."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Literal, Optional

from gc_core.constants.tool_display import (
    DisplayCategory,
    McpToolInfo,
    tool_display,
)

from gc_tui.models import TraceEvent
from gc_tui.services.duration_format import format_round_int
from gc_tui.services.skill_view import skill_input_name
from gc_tui.services.trace_retention import (
    apply_payload_retention,
    filter_sidecar_to_parents,
    message_ids_in_retention_window,
    retention_window_start_index,
)

if TYPE_CHECKING:
    from rich.text import Text

    from gc_tui.services.trace_mapper import RenderContext
    from gc_tui.widgets.consequence_line import ConsequenceLine
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from gc_tui.widgets.trace_row import (
        CompactionBody,
        CompactionRule,
        TraceRow,
    )

# Парсер `[tag] rest` для default meta-кейса (трейс и read-only превью).
TAG_RE = re.compile(r"^\[(?P<tag>[^\]]+)\]\s*(?P<rest>.*)$")

# Уровень вложенности события — единственный источник отступа ленты: глиф
# события уровня n стоит в колонке `●` реплики агента плюс 2n. Живой трейс
# (TraceScreen._mount_nested_*, main.py) и read-only превью
# (merge_nested_readonly ниже) берут отступ отсюда.
LEVEL_INDENT = "  "


def level_prefix(level: int) -> str:
    """Отступ события уровня ``level`` (0 — верхний уровень, без отступа)."""
    return LEVEL_INDENT * level


# Глифы колонки `●`: реплика агента (итоговая — белым, промежуточная —
# серым по классу `line-intermediate`) и реплика пользователя.
PROSE_BULLET = "● "
USER_PREFIX = "› "
NESTED_PROSE_PREFIX = level_prefix(1) + PROSE_BULLET
NESTED_USER_PREFIX = level_prefix(1) + USER_PREFIX
LINE_INTERMEDIATE_CLASS = "line-intermediate"
# Вложенная реплика субагента: глиф `●` серым (CSS по классу).
NESTED_PROSE_CLASS = f"{LINE_INTERMEDIATE_CLASS} nested-prose"
LINE_USER_CLASS = "line-user"

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
# цвет — классом важности; `✗` остаётся только статусом вызова.
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


def note_consequence(text: str) -> Text:
    """Тело следствия-пометки под владельцем: `! текст` цветом `WARN`
    (строку `└` дорисует владелец — `add_consequence`)."""
    from rich.text import Text

    from gc_tui.palette import WARN

    body = Text()
    body.append(STANDALONE_PREFIX + text, style=WARN)
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
    label = next((x for x in labels if cell_len(x) <= budget), None)
    if label is None:
        label = Text(labels[-1])
        label.truncate(max(budget, 1), overflow="ellipsis")
        label = label.plain
    middle = f" {arrow} {label} "
    bars = max(width - cell_len(middle), 2 * _RULE_MIN)
    text = Text()
    text.append(_RULE * (bars // 2), style=FRAME_ARCHIVE)
    text.append(middle, style=DIM)
    text.append(_RULE * (bars - bars // 2), style=FRAME_ARCHIVE)
    return text


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
    kind_class_base: str = "line-agent",
) -> list:
    """Markdown → виджеты трейса. `prose_bullet` — префикс `● ` на прозе
    (трейс/превью); False — без него (баннер ask_human). `kind_class_base` —
    класс прозовых строк (intermediate_reply → line-intermediate, глиф
    серым). Код и таблицы — BlockPanel. focusable=True только у первого
    виджета события."""
    from gc_tui.services.markdown_render import render_prose, segment_answer
    from gc_tui.widgets.block_panel import BlockPanel
    from gc_tui.widgets.trace_row import TraceRow

    widgets: list = []
    focus_set = False
    first_prose_seen = False
    for seg in segment_answer(text):
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
        marker = PROSE_BULLET if not first_prose_seen else "  "
        prefix = marker if prose_bullet else ""
        first_prose_seen = True
        widgets.append(
            TraceRow(
                prefix=prefix,
                content=render_prose(seg.content),
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
    from rich.text import Text

    from gc_tui.palette import DIM

    question.add_consequence(Text(note, style=DIM))


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


def note_text(comment: str) -> Text:
    """Контент строки заметки submit_solution: ярлык «заметка: » прямой, текст
    ремарки — курсивом (цвет/отступ берутся из CSS `.line-note`)."""
    from rich.text import Text

    t = Text("заметка: ")
    t.append(comment, style="italic")
    return t


def tool_caption(
    wire_name: str,
    mcp_tools: Mapping[str, McpToolInfo] | None = None,
    mcp_servers: Iterable[str] = (),
) -> tuple[DisplayCategory, str]:
    """Категория и имя тула для ленты по отображению ядра (спека P3 §2.1).

    MCP — `сервер · тул`; тул без подписи (неизвестный или скрытый
    политикой) — сырое wire-имя; остальные — подпись ядра."""
    shown = tool_display(
        wire_name, mcp_tools=mcp_tools, mcp_servers=mcp_servers
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
) -> tuple[str, str]:
    """(подпись, объект) строки вызова: тул без подписи (неизвестный или
    скрытый, но всё же дошедший до ленты) — `Called` и сырое имя в
    объекте."""
    category, name = tool_caption(wire_name, mcp_tools, mcp_servers)
    if category in (DisplayCategory.CALLED, DisplayCategory.HIDDEN):
        target = " ".join(p for p in (name, args_summary) if p)
        return DisplayCategory.CALLED.value, target
    return name, args_summary


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
) -> ToolCallRow:
    """Строка вызова — один билдер для живой ленты, превью и вложенных."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    label, target = _call_caption(
        wire_name, args_summary, mcp_tools, mcp_servers
    )
    return ToolCallRow(
        call_id=call_id,
        label=label,
        target=target,
        wire_name=wire_name,
        event_no=event_no,
        waiting=waiting,
        level=level,
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


def _mount_compaction(mount_target, ev: TraceEvent) -> None:
    """Метка сжатия и тело в read-only превью. Метка не focusable: иначе
    перехватывала бы Enter (в превью Enter — открыть тред); раскрытие —
    кликом (`PreviewScreen.on_summary_toggle_request`)."""
    rule, body = build_compaction_widgets(ev, focusable=False)
    mount_target.mount(rule)
    if body is not None:
        mount_target.mount(body)


def _dispatch_readonly(
    mount_target,
    ev,
    calls: dict,
    *,
    mcp_tools: Mapping[str, McpToolInfo] | None = None,
    mcp_servers: Iterable[str] = (),
    questions: dict | None = None,
) -> None:
    """Смонтировать виджеты одного события (read-only паритет с _render_event).

    ``questions`` — `{call_id: QuestionRow}` вопросов превью: ответ «без
    ответа» дописывается под свой вопрос, а не отдельной строкой."""
    from gc_tui.widgets.trace_row import TraceRow
    from gc_tui.widgets.user_reply import UserReplyContainer

    if ev.kind == "user":
        mount_target.mount(
            UserReplyContainer(
                ev.summary,
                event_no=ev.no,
                images=ev.attachments,
                # Каталога у превью нет — короткое имя (спека P3 §2.3).
                skill=skill_input_name(ev.skill) if ev.skill else None,
            )
        )
    elif ev.kind == "agent":
        if ev.tool_name == "intermediate_reply":
            for w in build_intermediate_widgets(ev.summary, event_no=ev.no):
                mount_target.mount(w)
            return
        for w in build_agent_widgets(ev.summary, event_no=ev.no):
            mount_target.mount(w)
        comment = (
            ev.full_payload.get("_submit_comment")
            if isinstance(ev.full_payload, dict)
            else None
        )
        if comment:
            mount_target.mount(
                TraceRow(
                    prefix="* ",
                    content=note_text(comment),
                    kind_class="line-note",
                    event_no=ev.no,
                    focusable=False,
                )
            )
    elif ev.kind == "call":
        row = build_call_row(
            call_id=ev.call_id or f"_anon_{ev.no}",
            wire_name=ev.wire_name or ev.tool_name or "<tool>",
            args_summary=ev.args_summary,
            level=0,
            event_no=ev.no,
            mcp_tools=mcp_tools,
            mcp_servers=mcp_servers,
        )
        calls[row.call_id] = row
        mount_target.mount(row)
    elif ev.kind in ("result", "error"):
        row = calls.get(ev.call_id) if ev.call_id else None
        if row is not None:
            row.set_result(
                summary=ev.summary,
                error=(ev.kind == "error"),
                halted=ev.halted,
            )
        elif ev.kind == "error":
            mount_target.mount(
                build_standalone_row(
                    ev.summary, severity="error", event_no=ev.no
                )
            )
        else:
            mount_target.mount(
                TraceRow(
                    prefix="   ",
                    content=ev.summary,
                    kind_class="line-ok",
                    event_no=ev.no,
                )
            )
    elif ev.kind == "summary":
        _mount_compaction(mount_target, ev)
    elif ev.kind == "error_msg":
        # Паритет с живым путём (TraceScreen._render_error_msg): ошибка с
        # известным call_id финализирует свою строку-вызов ✗ и не дублируется
        # строкой `!`. Иначе строка навсегда осталась бы «выполняется»:
        # схемные ошибки приходят техническим error_message с call_id, а не
        # function_call_output.
        err_row = calls.get(ev.call_id) if ev.call_id else None
        if err_row is not None:
            err_row.set_result(summary=ev.summary, error=True)
            calls.pop(ev.call_id, None)
        else:
            mount_target.mount(
                build_standalone_row(
                    ev.summary, severity="error", event_no=ev.no
                )
            )
    elif ev.kind == "ask_human":
        question = build_answered_question_widget(
            ev.summary, event_no=ev.no, call_id=ev.call_id
        )
        if questions is not None and ev.call_id:
            questions[ev.call_id] = question
        mount_target.mount(question)
    elif ev.kind == "todo":
        # Канон spec 2026-07-31 §2: строки собирает общий билдер (он же у
        # живого канала в main.py), здесь только монтируем.
        from gc_core.execution.progress import todo_items_from_arguments

        from gc_tui.services.todo_render import build_todo_block

        items = todo_items_from_arguments(
            (ev.full_payload or {}).get("arguments")
        )
        mount_target.mount(
            build_todo_block(items, subagent=False, indent="", event_no=ev.no)
        )
    else:
        question = (
            (questions or {}).get(ev.call_id)
            if ev.tool_name == "ask_human"
            else None
        )
        if question is not None:
            note_question(question, QUESTION_UNANSWERED)
            return
        m = TAG_RE.match(ev.summary or "")
        if m:
            mount_target.mount(
                TraceRow(
                    prefix=f"[{m.group('tag')}] ",
                    content=m.group("rest"),
                    kind_class="line-meta",
                    event_no=ev.no,
                )
            )
        else:
            mount_target.mount(
                TraceRow(
                    prefix="",
                    content=ev.summary or "",
                    kind_class="line-meta",
                    event_no=ev.no,
                )
            )


def _render_record_readonly(
    mount_target,
    rec: dict,
    no: int,
    calls: dict,
    context: RenderContext | None = None,
    *,
    in_window: bool = True,
    mcp_tools: Mapping[str, McpToolInfo] | None = None,
    mcp_servers: Iterable[str] = (),
    questions: dict | None = None,
) -> int:
    """Одна envelope-запись → виджеты. Возвращает следующий no.
    hidden-события (policy/bounce/context) считаем, но в трейс не рендерим —
    как _render_event на основном экране."""
    from gc_tui.services.trace_mapper import map_message

    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return no
    ev = map_message(payload, no=no, ts=rec.get("ts", ""), context=context)
    if ev is None:
        return no
    apply_payload_retention(ev, seg_in_window=in_window)
    no += 1
    if ev.hidden:
        return no
    _dispatch_readonly(
        mount_target,
        ev,
        calls,
        mcp_tools=mcp_tools,
        mcp_servers=mcp_servers,
        questions=questions,
    )
    return no


def render_segments_readonly(
    mount_target,
    segments: list,
    *,
    mcp_tools: Mapping[str, McpToolInfo] | None = None,
    mcp_servers: Iterable[str] = (),
) -> dict:
    """Read-only превью из сегментов: полная история (все сегменты сырьём) +
    нумерованные маркеры сжатий #K между ними. Паритет рендера с основным
    трейсом (hidden/policy скрыто, summary/error_msg/intermediate как там).
    Возвращает `calls` (`{call_id: ToolCallRow}`) — нужен вызывающему для
    домонтирования вложенного трейса субагентов (см. `merge_nested_readonly`)."""
    from gc_tui.services.trace_mapper import collect_render_context

    no = 1
    calls: dict = {}
    questions: dict = {}
    context = collect_render_context(
        rec.get("payload")
        for seg in segments
        for rec in seg.get("messages", [])
        if isinstance(rec, dict)
    )
    # Дедуп по id: preserved-копия (user и пара call/output) лежит в следующем
    # файле с тем же id. Рисуем первое вхождение. Payload full, пока id есть
    # в окне segments[-2:].
    seen_ids: set = set()
    window_start = retention_window_start_index(len(segments))
    window_ids = message_ids_in_retention_window(segments)
    for i, seg in enumerate(segments):
        seg_in_window = i >= window_start
        for rec in seg.get("messages", []):
            payload = rec.get("payload") if isinstance(rec, dict) else None
            mid = payload.get("id") if isinstance(payload, dict) else None
            if mid is not None:
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
            in_window = seg_in_window or (
                mid is not None and mid in window_ids
            )
            no = _render_record_readonly(
                mount_target,
                rec,
                no,
                calls,
                context,
                in_window=in_window,
                mcp_tools=mcp_tools,
                mcp_servers=mcp_servers,
                questions=questions,
            )
        comp = seg.get("compacted_by")
        if comp:
            _mount_compaction(
                mount_target,
                build_compaction_marker_event(no, seg.get("index", 0), comp),
            )
            no += 1
    return calls


def merge_nested_readonly(
    mount_target,
    records: list,
    calls: dict,
    *,
    allowed_parent_ids: set[str] | None = None,
    mcp_tools: Mapping[str, McpToolInfo] | None = None,
    mcp_servers: Iterable[str] = (),
) -> None:
    """Read-only домонтирование вложенного трейса субагентов из sidecar-records
    (превью). Паритет с MainScreen._merge_sidecar_nested: те же plan/apply,
    якорение «после parent или последнего nested» — локальными anchors.

    ``allowed_parent_ids`` — parent'ы из окна retention. None оставляет все
    записи (вызывающий без сегментов). Полный sidecar после вызова не хранить.
    """
    if allowed_parent_ids is not None:
        records = filter_sidecar_to_parents(records, allowed_parent_ids)
    from gc_tui.services.subagent_sidecar_mapper import (
        CHILD_SUMMARY_LABEL,
        apply_sidecar_nested_actions,
        plan_sidecar_nested_actions,
    )
    from gc_tui.services.todo_render import build_todo_block
    from gc_tui.widgets.trace_row import TraceRow
    from gc_tui.widgets.user_reply import NestedUserReply

    anchors: dict = {}  # parent_call_id -> последний смонтированный nested
    nested_rows: dict = {}  # tool_call_id -> ToolCallRow (для finish)

    def _mount_after(pid: str, widget) -> None:
        parent = calls.get(pid)
        if parent is None:
            return
        anchor = anchors.get(pid, parent)
        try:
            mount_target.mount(widget, after=anchor)
        except Exception:
            mount_target.mount(widget)
        anchors[pid] = widget

    def _tool(*, parent_call_id, tool_name, tool_call_id, args_summary, **_kw):
        row = build_call_row(
            call_id=tool_call_id or f"_nested_{tool_name}",
            wire_name=tool_name,
            args_summary=args_summary or "",
            level=1,
            mcp_tools=mcp_tools,
            mcp_servers=mcp_servers,
        )
        if tool_call_id:
            nested_rows[tool_call_id] = row
        _mount_after(parent_call_id, row)

    def _finish(
        *,
        parent_call_id,  # noqa: ARG001  # параметр контракта callback
        tool_call_id="",
        ok=True,
        error_brief=None,
        halted=False,
        **_kw,
    ):
        row = nested_rows.get(tool_call_id)
        if row is not None:
            row.set_result(
                summary="" if ok else (error_brief or "error"),
                error=not ok,
                halted=halted,
            )

    def _prose(*, parent_call_id, prose, **_kw):
        text = (prose or "").strip()
        if not text:
            return
        _mount_after(
            parent_call_id,
            TraceRow(
                prefix=NESTED_PROSE_PREFIX,
                content=text,
                kind_class=NESTED_PROSE_CLASS,
                event_no=0,
                focusable=False,
            ),
        )

    def _user(*, parent_call_id, prose, images=(), **_kw):
        text = (prose or "").strip()
        if not text:
            return
        _mount_after(parent_call_id, NestedUserReply(text, images=images))

    def _child_summary(*, parent_call_id, prose="", **_kw):
        _prose(
            parent_call_id=parent_call_id, prose=prose or CHILD_SUMMARY_LABEL
        )

    def _todo(*, parent_call_id, tool_call_id="", todo_items=None, **_kw):  # noqa: ARG001  # часть контракта: подпись менять нельзя
        _mount_after(
            parent_call_id,
            build_todo_block(
                todo_items or [], subagent=True, indent=level_prefix(1)
            ),
        )

    apply_sidecar_nested_actions(
        plan_sidecar_nested_actions(records),
        mount_nested_tool=_tool,
        finish_nested_tool=_finish,
        mount_nested_prose=_prose,
        mount_nested_user=_user,
        mount_nested_summary=_child_summary,
        mount_nested_todo=_todo,
    )
