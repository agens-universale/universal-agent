"""Общие строители виджетов трейса — переиспользуются главным экраном (через
делегирование из TraceScreen._render_event) и read-only превью.

Чистые: конструируют и возвращают виджеты, не монтируют, не трогают экран/состояние."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Optional

from gc_tui.models import TraceEvent
from gc_tui.palette import INK
from gc_tui.services.duration_format import format_compact_int
from gc_tui.services.trace_retention import (
    apply_payload_retention,
    filter_sidecar_to_parents,
    message_ids_in_retention_window,
    retention_window_start_index,
)

if TYPE_CHECKING:
    from gc_core.tools.turn.turn_close import TurnClose
    from rich.text import Text

    from gc_tui.services.trace_mapper import RenderContext

# Парсер `[tag] rest` для default meta-кейса (трейс и read-only превью).
TAG_RE = re.compile(r"^\[(?P<tag>[^\]]+)\]\s*(?P<rest>.*)$")

# Общие литералы вложенных (nested) строк под subagent_* — одинаковы в живом
# трейсе (TraceScreen._mount_nested_*, main.py) и read-only превью
# (merge_nested_readonly ниже).
NESTED_PREFIX = "       "  # nested под subagent: +4 пробела к _PREFIX тула
NESTED_PROSE_PREFIX = "       · "
NESTED_USER_PREFIX = "       > "
LINE_INTERMEDIATE_CLASS = "line-intermediate"
LINE_USER_CLASS = "line-user"
LINE_SUMMARY_CLASS = "line-summary"
LINE_NOTE_CLASS = "line-note"
# Метка записи конца хода жирная и прямая — и в курсивной строке итога.
_RECORD_LABEL_STYLE = "bold not italic"


def build_compaction_label(num: int, comp: dict) -> str:
    """Метка маркера сжатия: «контекст сжат #N · M→1 · 12.3k→3.1k симв».

    Суффикс символов опускается, если chars_before/after отсутствуют (legacy).
    """
    label = f"контекст сжат #{num}"
    msgs = comp.get("messages_in_segment")
    if msgs:
        label += f" · {msgs}→1"
    cb, ca = comp.get("chars_before"), comp.get("chars_after")
    if cb and ca:
        label += f" · {format_compact_int(cb)}→{format_compact_int(ca)} симв"
    return label


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
    """
    return TraceEvent(
        no=no,
        event_ts_utc_iso=comp.get("ts", ""),
        kind="summary",
        title="summary",
        summary=build_compaction_label(segment_index + 1, comp),
        summary_content=comp["synthesis"],
        full_payload={},
    )


def build_block_widgets(
    text: str,
    *,
    prose_bullet: bool,
    event_no: int,
    bullet: str = "● ",
    kind_class_base: str = "line-agent",
    ink: str = INK,
    lead: Optional[Text] = None,
) -> list:
    """Markdown → виджеты трейса. `prose_bullet` — префикс `bullet` на прозе (трейс/превью);
    False — без него (баннер ask_human). `bullet`/`kind_class_base` переопределяют маркер и
    класс прозовых строк (intermediate_reply → «· »/line-intermediate). Код и таблицы —
    BlockPanel. focusable=True только у первого виджета события.
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
    question, *, event_no: int = 0, extra_classes: str = ""
):
    """Отвеченный вопрос ask_human как строка истории: серая round-рамка
    (CSS .answered-question) + янтарный маркер '?'. Тех-префикс 'ask_human:'
    не добавляем. Общий для live (main) и readonly (preview) путей.

    Цвет рамки — токен $frame-archive (серый). Альтернатива (живое сравнение
    после релиза) — янтарная рамка: сменить border в .answered-question на
    $run-color, точка правки одна."""
    from rich.text import Text

    from gc_tui.palette import DIM, RUN_COLOR
    from gc_tui.widgets.trace_row import TraceRow

    content = Text()
    content.append("? ", style=RUN_COLOR)
    content.append(question, style=DIM)
    cls = ("answered-question " + extra_classes).strip()
    return TraceRow(
        prefix="",
        content=content,
        kind_class=cls,
        event_no=event_no,
        focusable=True,
    )


def is_tool_group_start(mount_target) -> bool:
    """True, если последний значимый ряд в `mount_target` — НЕ tool-вызов: тогда
    эта tool-строка открывает новую группу и получает верхний отступ (CSS
    `.tool-group-start`). Подряд идущие tool-строки остаются компактными.
    Общий для live (main) и readonly (preview) путей."""
    from gc_tui.widgets.diff_block import DiffBlock
    from gc_tui.widgets.tool_call_row import ToolCallRow

    for child in reversed(list(mount_target.children)):
        if getattr(child, "id", None) == "brand-block":
            continue  # шапка трейса — не контент
        if isinstance(child, DiffBlock):
            continue  # инлайн-дифф принадлежит tool-строке выше — группу не рвёт
        return not isinstance(child, ToolCallRow)
    return True  # перед ним ничего — считаем стартом группы


def build_agent_widgets(summary: str, *, event_no: int) -> list:
    """Ответ агента в трейсе/превью: проза с `● `, код панелями, таблицы сеткой."""
    return build_block_widgets(summary, prose_bullet=True, event_no=event_no)


def build_intermediate_widgets(summary: str, *, event_no: int) -> list:
    """·-реплика intermediate_reply: тот же markdown/код-пайплайн, что и проза
    (build_agent_widgets), но маркер «· » и класс line-intermediate."""
    return build_block_widgets(
        summary,
        prose_bullet=True,
        event_no=event_no,
        bullet="· ",
        kind_class_base="line-intermediate",
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
        # Оговорка — второй строкой той же записи, без пустой строки.
        text = "\n".join(
            part for part in (close.note, close.comment) if part != ""
        )
        return build_summary_widgets(text, event_no=event_no)
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


def args_portion(summary: str) -> str:
    """Аргументы из summary вида "name(args)" — содержимое скобок."""
    if summary and "(" in summary:
        return summary.split("(", 1)[1].rstrip(")")
    return ""


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


def _mount_summary_widgets(
    mount_target, label: str, body_text: str, event_no: int
) -> None:
    """Маркер сжатия (свёрнут) + тело (.hidden) для read-only превью.
    Toggle — ТОЛЬКО по клику мышью (on_click постит SummaryToggleRequest;
    обработчик — PreviewScreen.on_summary_toggle_request). Маркер НЕ focusable:
    иначе он перехватывал бы Enter (toggle), а в превью Enter = открыть тред.
    Маркер и тело делят event_no, чтобы toggle находил тело по нему."""
    from gc_tui.widgets.trace_row import SummaryMarker

    mount_target.mount(
        SummaryMarker(
            prefix="▸ ",
            content=f"──── {label} ────",
            kind_class="summary-marker",
            event_no=event_no,
            focusable=False,
        )
    )
    if body_text:
        mount_target.mount(
            SummaryMarker(
                prefix="   ",
                content=f"Контекст сохранён:\n{body_text}",
                kind_class="summary-body hidden",
                event_no=event_no,
                focusable=False,
            )
        )


def _dispatch_readonly(mount_target, ev, calls: dict) -> None:
    """Смонтировать виджеты одного события (read-only паритет с _render_event)."""
    from gc_tui.widgets.tool_call_row import ToolCallRow
    from gc_tui.widgets.trace_row import TraceRow
    from gc_tui.widgets.user_reply import UserReplyContainer

    if ev.kind == "user":
        mount_target.mount(
            UserReplyContainer(
                ev.summary, event_no=ev.no, images=ev.attachments
            )
        )
    elif ev.kind == "agent":
        if ev.tool_name == "intermediate_reply":
            for w in build_intermediate_widgets(ev.summary, event_no=ev.no):
                mount_target.mount(w)
            return
        widgets = (
            build_finish_widgets(finish_close_of(ev), event_no=ev.no)
            if ev.tool_name == "submit_solution"
            else build_agent_widgets(ev.summary, event_no=ev.no)
        )
        for w in widgets:
            mount_target.mount(w)
    elif ev.kind == "call":
        row = ToolCallRow(
            call_id=ev.call_id or f"_anon_{ev.no}",
            # Показ — wire-имя реестра (Д11/R3); tool_name(канонический id) —
            # только фолбэк на случай, если wire_name не заполнен.
            tool_name=ev.wire_name or ev.tool_name or "<tool>",
            args_summary=args_portion(ev.summary),
            event_no=ev.no,
        )
        if is_tool_group_start(mount_target):
            row.add_class(
                "tool-group-start"
            )  # первый tool в группе — верхний отступ
        calls[row.call_id] = row
        mount_target.mount(row)
    elif ev.kind in ("result", "error"):
        row = calls.get(ev.call_id) if ev.call_id else None
        if row is not None:
            row.set_result(summary=ev.summary, error=(ev.kind == "error"))
        else:
            prefix = "   " if ev.kind == "result" else "   [!] "
            cls = "line-ok" if ev.kind == "result" else "line-error"
            mount_target.mount(
                TraceRow(
                    prefix=prefix,
                    content=ev.summary,
                    kind_class=cls,
                    event_no=ev.no,
                )
            )
    elif ev.kind == "summary":
        _mount_summary_widgets(
            mount_target,
            ev.summary or "контекст сжат",
            (ev.summary_content or "").strip(),
            ev.no,
        )
    elif ev.kind == "error_msg":
        # Паритет с живым путём (TraceScreen._render_error_msg): ошибка с известным
        # call_id финализирует свою строку-вызов ✗ и не дублируется [!]-строкой.
        # Иначе строка навсегда осталась бы «выполняется»: схемные ошибки приходят
        # техническим error_message с call_id, а не function_call_output.
        err_row = calls.get(ev.call_id) if ev.call_id else None
        if err_row is not None:
            err_row.set_result(summary=ev.summary, error=True)
            calls.pop(ev.call_id, None)
        else:
            mount_target.mount(
                TraceRow(
                    prefix="[!] ",
                    content=ev.summary,
                    kind_class="line-error",
                    event_no=ev.no,
                )
            )
    elif ev.kind == "ask_human":
        mount_target.mount(
            build_answered_question_widget(ev.summary, event_no=ev.no)
        )
    elif ev.kind == "todo":
        # Канон spec 2026-07-31 §2: строки собирает общий билдер (он же у
        # живого канала в main.py), здесь только монтируем.
        from gc_core.execution.progress import todo_items_from_arguments

        from gc_tui.services.todo_render import build_todo_block

        items = todo_items_from_arguments(
            (ev.full_payload or {}).get("arguments")
        )
        mount_target.mount(
            build_todo_block(
                items, subagent=False, indent="   ", event_no=ev.no
            )
        )
    else:
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


def _mount_skill_marker_readonly(
    mount_target, skill_id: str, event_no: int
) -> None:
    """[skill] <id> загружен — паритет с MainScreen._mount_skill_marker (read-only).
    skill_loaded ставится только когда скилл реально загрузился → всегда «загружен»."""
    from gc_tui.widgets.trace_row import TraceRow

    row = TraceRow(
        prefix="[skill] ",
        content=f"`{skill_id}` загружен",
        kind_class="line-meta",
        event_no=event_no,
        focusable=False,
    )
    row.add_class("skill-marker")
    mount_target.mount(row)


def _render_record_readonly(
    mount_target,
    rec: dict,
    no: int,
    calls: dict,
    context: RenderContext | None = None,
    *,
    in_window: bool = True,
) -> int:
    """Одна envelope-запись → виджеты. Возвращает следующий no.
    hidden-события (policy/bounce/context) считаем, но в трейс не рендерим —
    как _render_event на основном экране."""
    from gc_tui.services.trace_mapper import map_message

    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return no
    ev = map_message(payload, no=no, ts=rec.get("ts", ""), context=context)
    if ev is not None:
        apply_payload_retention(ev, seg_in_window=in_window)
    # skill_loaded извлекаем ДО early-return: mid-run доставка (display_content="")
    # даёт ev=None, но маркер [skill] всё равно нужен в превью.
    sk = payload.get("skill_loaded")
    if ev is None:
        if sk:
            _mount_skill_marker_readonly(
                mount_target, sk, no
            )  # mid-run: только маркер
        return no
    no += 1
    if ev.hidden:
        return no
    _dispatch_readonly(mount_target, ev, calls)
    if sk:
        _mount_skill_marker_readonly(mount_target, sk, no)  # после реплики
    return no


def render_segments_readonly(mount_target, segments: list) -> dict:
    """Read-only превью из сегментов: полная история (все сегменты сырьём) +
    нумерованные маркеры сжатий #K между ними. Паритет рендера с основным
    трейсом (hidden/policy скрыто, summary/error_msg/intermediate как там).
    Возвращает `calls` (`{call_id: ToolCallRow}`) — нужен вызывающему для
    домонтирования вложенного трейса субагентов (см. `merge_nested_readonly`)."""
    from gc_tui.services.trace_mapper import collect_render_context

    no = 1
    calls: dict = {}
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
            )
        comp = seg.get("compacted_by")
        if comp:
            label = build_compaction_label(seg.get("index", 0) + 1, comp)
            _mount_summary_widgets(
                mount_target, label, (comp.get("synthesis") or "").strip(), no
            )
            no += 1
    return calls


def merge_nested_readonly(
    mount_target,
    records: list,
    calls: dict,
    *,
    allowed_parent_ids: set[str] | None = None,
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
    from gc_tui.widgets.tool_call_row import ToolCallRow
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
        row = ToolCallRow(
            call_id=tool_call_id or f"_nested_{tool_name}",
            tool_name=tool_name,
            args_summary=args_summary or "",
            nested=True,
            waiting=False,
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
        **_kw,
    ):
        row = nested_rows.get(tool_call_id)
        if row is not None:
            row.set_result(
                summary="" if ok else (error_brief or "error"), error=not ok
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
                kind_class=LINE_INTERMEDIATE_CLASS,
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
                todo_items or [], subagent=True, indent=NESTED_PREFIX
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
