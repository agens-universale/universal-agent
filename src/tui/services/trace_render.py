"""Общие строители виджетов трейса — переиспользуются главным экраном (через
делегирование из TraceScreen._render_event) и read-only превью.

Чистые: конструируют и возвращают виджеты, не монтируют, не трогают экран/состояние."""
from __future__ import annotations

import json
import re
from typing import Optional

from tui.palette import BAND_BG, CODE_BLOCK
from tui.widgets.agent_reply_line import AgentReplyLine
from tui.services.duration_format import format_compact_int

# Парсер `[tag] rest` для default meta-кейса (трейс и read-only превью).
TAG_RE = re.compile(r"^\[(?P<tag>[^\]]+)\]\s*(?P<rest>.*)$")


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


def render_code_block(content: str, lang: Optional[str], *,
                      event_no: int, focusable: bool = False,
                      indent: bool = False) -> "AgentReplyLine":
    """AgentReplyLine с подсветкой (rich.syntax) или fallback на $code-block-цвет.
    CSS-класс `.code-block-panel`. `indent` (трейс/превью) добавляет `.code-indent`
    → отступ под текст реплики; без него (баннер) — вровень с прозой на колонке 0."""
    from rich.syntax import Syntax
    from rich.text import Text
    from pygments.lexers import get_lexer_by_name
    from pygments.util import ClassNotFound

    cls = "code-block-panel code-indent" if indent else "code-block-panel"
    if lang:
        try:
            get_lexer_by_name(lang)
            syntax = Syntax(
                content, lexer=lang, theme="monokai",
                background_color=BAND_BG, word_wrap=False,
            )
            return AgentReplyLine(
                syntax, event_no=event_no, focusable=focusable, classes=cls,
            )
        except ClassNotFound:
            pass
    return AgentReplyLine(
        Text(content, style=CODE_BLOCK),
        event_no=event_no, focusable=focusable, classes=cls,
    )


def build_block_widgets(text: str, *, prose_bullet: bool, event_no: int,
                        bullet: str = "● ", kind_class_base: str = "line-agent") -> list:
    """Markdown → виджеты трейса. `prose_bullet` — префикс `bullet` на прозе (трейс/превью);
    False — без него (баннер ask_human). `bullet`/`kind_class_base` переопределяют маркер и
    класс прозовых строк (intermediate_reply → «· »/line-intermediate). Код всегда панель,
    таблицы — AgentReplyLine(.table-block). focusable=True только у первого виджета события."""
    from tui.services.markdown_render import (
        build_markdown_table, render_prose, segment_answer,
    )
    from tui.widgets.trace_row import TraceRow

    widgets: list = []
    focus_set = False
    first_prose_seen = False
    for seg in segment_answer(text):
        if seg.kind == "code-block":
            widgets.append(render_code_block(
                seg.content, seg.lang, event_no=event_no,
                focusable=not focus_set, indent=prose_bullet,
            ))
            focus_set = True
            continue
        if seg.kind == "table":
            table_classes = "table-block table-indent" if prose_bullet else "table-block"
            widgets.append(AgentReplyLine(
                build_markdown_table(seg.headers or [], seg.rows or [], seg.aligns or []),
                event_no=event_no, focusable=not focus_set, classes=table_classes,
            ))
            focus_set = True
            continue
        kind_class = kind_class_base if not first_prose_seen else f"{kind_class_base} line-agent-cont"
        prefix = (bullet if not first_prose_seen else "  ") if prose_bullet else ""
        first_prose_seen = True
        widgets.append(TraceRow(
            prefix=prefix, content=render_prose(seg.content),
            kind_class=kind_class, event_no=event_no, focusable=not focus_set,
        ))
        focus_set = True
    return widgets


def build_answered_question_widget(question, *, event_no: int = 0,
                                   extra_classes: str = ""):
    """Отвеченный вопрос ask_human как строка истории: серая round-рамка
    (CSS .answered-question) + янтарный маркер '?'. Тех-префикс 'ask_human:'
    не добавляем. Общий для live (main) и readonly (preview) путей.

    Цвет рамки — токен $frame-archive (серый). Альтернатива (живое сравнение
    после релиза) — янтарная рамка: сменить border в .answered-question на
    $run-color, точка правки одна."""
    from rich.text import Text
    from tui.widgets.trace_row import TraceRow
    from tui.palette import RUN_COLOR, DIM
    content = Text()
    content.append("? ", style=RUN_COLOR)
    content.append(question, style=DIM)
    cls = ("answered-question " + extra_classes).strip()
    return TraceRow(prefix="", content=content, kind_class=cls,
                    event_no=event_no, focusable=True)


def is_tool_group_start(mount_target) -> bool:
    """True, если последний значимый ряд в `mount_target` — НЕ tool-вызов: тогда
    эта tool-строка открывает новую группу и получает верхний отступ (CSS
    `.tool-group-start`). Подряд идущие tool-строки остаются компактными.
    Общий для live (main) и readonly (preview) путей."""
    from tui.widgets.tool_call_row import ToolCallRow
    for child in reversed(list(mount_target.children)):
        if getattr(child, "id", None) == "brand-block":
            continue  # шапка трейса — не контент
        if child.has_class("tool-result-preview"):
            continue  # hidden-превью результата — часть tool-строки выше
        return not isinstance(child, ToolCallRow)
    return True   # перед ним ничего — считаем стартом группы


def build_agent_widgets(summary: str, *, event_no: int) -> list:
    """Ответ агента в трейсе/превью: проза с `● `, код панелями, таблицы сеткой."""
    return build_block_widgets(summary, prose_bullet=True, event_no=event_no)


def build_intermediate_widgets(summary: str, *, event_no: int) -> list:
    """·-реплика intermediate_reply: тот же markdown/код-пайплайн, что и проза
    (build_agent_widgets), но маркер «· » и класс line-intermediate."""
    return build_block_widgets(summary, prose_bullet=True, event_no=event_no,
                               bullet="· ", kind_class_base="line-intermediate")


def note_text(comment: str) -> "Text":
    """Контент строки заметки submit_solution: ярлык «заметка: » прямой, текст
    ремарки — курсивом (цвет/отступ берутся из CSS `.line-note`)."""
    from rich.text import Text
    t = Text("заметка: ")
    t.append(comment, style="italic")
    return t


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


def _mount_summary_widgets(mount_target, label: str, body_text: str,
                           event_no: int) -> None:
    """Маркер сжатия (свёрнут) + тело (.hidden) для read-only превью.
    Toggle — ТОЛЬКО по клику мышью (on_click постит SummaryToggleRequest;
    обработчик — PreviewScreen.on_summary_toggle_request). Маркер НЕ focusable:
    иначе он перехватывал бы Enter (toggle), а в превью Enter = открыть тред.
    Маркер и тело делят event_no, чтобы toggle находил тело по нему."""
    from tui.widgets.trace_row import SummaryMarker
    mount_target.mount(SummaryMarker(
        prefix="▸ ", content=f"──── {label} ────",
        kind_class="summary-marker", event_no=event_no, focusable=False))
    if body_text:
        mount_target.mount(SummaryMarker(
            prefix="   ", content=f"Контекст сохранён:\n{body_text}",
            kind_class="summary-body hidden", event_no=event_no, focusable=False))


def _dispatch_readonly(mount_target, ev, calls: dict) -> None:
    """Смонтировать виджеты одного события (read-only паритет с _render_event)."""
    from tui.widgets.trace_row import TraceRow
    from tui.widgets.tool_call_row import ToolCallRow
    from tui.widgets.user_reply import UserReplyContainer

    if ev.kind == "user":
        mount_target.mount(UserReplyContainer(ev.summary, event_no=ev.no))
    elif ev.kind == "agent":
        if ev.tool_name == "intermediate_reply":
            for w in build_intermediate_widgets(ev.summary, event_no=ev.no):
                mount_target.mount(w)
            return
        for w in build_agent_widgets(ev.summary, event_no=ev.no):
            mount_target.mount(w)
        comment = (ev.full_payload.get("_submit_comment")
                   if isinstance(ev.full_payload, dict) else None)
        if comment:
            mount_target.mount(TraceRow(
                prefix="* ", content=note_text(comment),
                kind_class="line-note", event_no=ev.no, focusable=False))
    elif ev.kind == "call":
        row = ToolCallRow(
            call_id=ev.call_id or f"_anon_{ev.no}",
            tool_name=ev.tool_name or "<tool>",
            args_summary=args_portion(ev.summary), event_no=ev.no)
        if is_tool_group_start(mount_target):
            row.add_class("tool-group-start")   # первый tool в группе — верхний отступ
        calls[row.call_id] = row
        mount_target.mount(row)
    elif ev.kind in ("result", "error"):
        row = calls.get(ev.call_id) if ev.call_id else None
        if row is not None:
            row.set_result(summary=ev.summary, error=(ev.kind == "error"))
        else:
            prefix = "  |- " if ev.kind == "result" else "  |- [!] "
            cls = "line-ok" if ev.kind == "result" else "line-error"
            mount_target.mount(TraceRow(prefix=prefix, content=ev.summary,
                                        kind_class=cls, event_no=ev.no))
    elif ev.kind == "summary":
        _mount_summary_widgets(
            mount_target, ev.summary or "контекст сжат",
            (ev.summary_content or "").strip(), ev.no)
    elif ev.kind == "error_msg":
        mount_target.mount(TraceRow(prefix="[!] ", content=ev.summary,
                                    kind_class="line-error", event_no=ev.no))
    elif ev.kind == "ask_human":
        mount_target.mount(build_answered_question_widget(ev.summary, event_no=ev.no))
    else:
        m = TAG_RE.match(ev.summary or "")
        if m:
            mount_target.mount(TraceRow(prefix=f"[{m.group('tag')}] ",
                                        content=m.group("rest"),
                                        kind_class="line-meta", event_no=ev.no))
        else:
            mount_target.mount(TraceRow(prefix="", content=ev.summary or "",
                                        kind_class="line-meta", event_no=ev.no))


def _mount_skill_marker_readonly(mount_target, skill_id: str, event_no: int) -> None:
    """[skill] <id> загружен — паритет с MainScreen._mount_skill_marker (read-only).
    skill_loaded ставится только когда скилл реально загрузился → всегда «загружен»."""
    from tui.widgets.trace_row import TraceRow
    row = TraceRow(prefix="[skill] ", content=f"`{skill_id}` загружен",
                   kind_class="line-meta", event_no=event_no, focusable=False)
    row.add_class("skill-marker")
    mount_target.mount(row)


def _render_record_readonly(mount_target, rec: dict, no: int, calls: dict) -> int:
    """Одна envelope-запись → виджеты. Возвращает следующий no.
    hidden-события (policy/bounce/context) считаем, но в трейс не рендерим —
    как _render_event на основном экране."""
    from tui.services.trace_mapper import map_message
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return no
    ev = map_message(payload, no=no, ts=rec.get("ts", ""))
    # skill_loaded извлекаем ДО early-return: mid-run доставка (display_content="")
    # даёт ev=None, но маркер [skill] всё равно нужен в превью.
    sk = payload.get("skill_loaded")
    if ev is None:
        if sk:
            _mount_skill_marker_readonly(mount_target, sk, no)   # mid-run: только маркер
        return no
    no += 1
    if ev.hidden:
        return no
    _dispatch_readonly(mount_target, ev, calls)
    if sk:
        _mount_skill_marker_readonly(mount_target, sk, no)       # после реплики
    return no


def render_thread_readonly(mount_target, records: list) -> None:
    """Построить и смонтировать виджеты трейса из плоских записей в mount_target —
    read-only (без pending/баннеров/submit-collapse/индикатора/tool-preview).
    submit_solution-output отфильтрован уже в map_message (→ None)."""
    no = 1
    calls: dict = {}
    for rec in records:
        no = _render_record_readonly(mount_target, rec, no, calls)


def render_segments_readonly(mount_target, segments: list) -> None:
    """Read-only превью из сегментов: полная история (все сегменты сырьём) +
    нумерованные маркеры сжатий #K между ними. Паритет рендера с основным
    трейсом (hidden/policy скрыто, summary/error_msg/intermediate как там)."""
    no = 1
    calls: dict = {}
    # Дедуп по id: preserved last-user-message лежит в двух соседних сегментах
    # (архив + live) — иначе реплика дублируется вокруг маркера сжатия.
    seen_ids: set = set()
    for seg in segments:
        for rec in seg.get("messages", []):
            payload = rec.get("payload") if isinstance(rec, dict) else None
            mid = payload.get("id") if isinstance(payload, dict) else None
            if mid is not None:
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
            no = _render_record_readonly(mount_target, rec, no, calls)
        comp = seg.get("compacted_by")
        if comp:
            label = build_compaction_label(seg.get("index", 0) + 1, comp)
            _mount_summary_widgets(
                mount_target, label, (comp.get("synthesis") or "").strip(), no)
            no += 1
