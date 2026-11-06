"""Map persisted data_subagents_* records → nested TUI mount actions / detail.

Display/audit only — does not touch agent state or parent LLM history.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Sequence,
    Tuple,
)

from gc_core.attachments.model import Attachment, attachments_of
from gc_core.constants.tool_display import DisplayCategory, tool_display
from gc_core.constants.tool_names import SESSION_TODO_TOOL_NAME
from gc_core.execution.progress import (
    parse_tool_result_status,
    run_command_text,
    tool_args_summary,
)

from gc_tui.models import ResultOutcome, TraceEvent
from gc_tui.services.command_output import parse_command_output
from gc_tui.services.trace_mapper import output_summary, result_stop

CHILD_SUMMARY_LABEL = "контекст субагента сжат"

SidecarActionKind = Literal[
    "mount_nested_tool",
    "finish_nested_tool",
    "mount_nested_prose",
    "mount_nested_user",
    "mount_nested_summary",
    "mount_nested_todo",
]


@dataclass(frozen=True)
class SidecarNestedAction:
    kind: SidecarActionKind
    parent_call_id: str
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    args_summary: Optional[str] = None
    prose: Optional[str] = None
    ok: Optional[bool] = None
    error_brief: Optional[str] = None
    # Не-сбой ребёнка: стоп или лимит (`■`), отказ (`○`); None — по `ok`.
    outcome: Optional[ResultOutcome] = None
    todo_items: Optional[List[Dict[str, str]]] = None
    # Картинки реплики человека ребёнку (``mount_nested_user``).
    images: tuple[Attachment, ...] = ()
    # Сырой текст команды Run-тула (``mount_nested_tool``) и сырой вывод
    # тула (``finish_nested_tool``) — как в событиях прогресса (спека P4
    # §7): строки команды, сводку и превью строит экран одним маппером.
    command: Optional[str] = None
    output: Optional[str] = None


def _raw_output(payload: Dict[str, Any]) -> Optional[str]:
    """Вывод записи результата как текст: не строка — `json.dumps`, как
    в событии `tool_end` (решение плана 12)."""
    output = payload.get("output")
    if output is None or isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False)


def nested_call_row(wire_name: str) -> bool:
    """Строка ли вложенный вызов — по имени, по вложенной таблице ядра:
    скрытый тул (`Hidden`) строкой не бывает, сдача субагента — бывает
    (последнее событие `Agent`). Чек-лист (`Hidden` в таблице) — строка:
    вызывающий спрашивает о нём, только когда не развернул вызов
    чек-листом (битые аргументы), и его сбой должен быть виден."""
    if wire_name == SESSION_TODO_TOOL_NAME:
        return True
    shown = tool_display(wire_name, nested=True)
    return shown.category is not DisplayCategory.HIDDEN


def _seq_key(record: Dict[str, Any]) -> int:
    seq = record.get("seq")
    return seq if isinstance(seq, int) else 0


def plan_sidecar_nested_actions(
    records: Sequence[Dict[str, Any]],
    *,
    root: Optional[str] = None,
) -> List[SidecarNestedAction]:
    """Group by parent_call_id, order by seq; skip finished/HITL-like noise.

    ``root`` — корень проекта: пути в подписях вызовов от него."""
    from gc_core.execution.progress import todo_items_from_arguments

    by_parent: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in records:
        if not isinstance(rec, dict):
            continue
        pid = rec.get("parent_call_id")
        if not isinstance(pid, str) or not pid:
            continue
        by_parent[pid].append(rec)

    actions: List[SidecarNestedAction] = []
    # Stable parent order = first appearance in file (min seq).
    parent_order = sorted(
        by_parent.keys(),
        key=lambda p: min(_seq_key(r) for r in by_parent[p]),
    )
    for pid in parent_order:
        rows = sorted(by_parent[pid], key=_seq_key)
        for rec in rows:
            kind = rec.get("kind")
            payload = rec.get("payload")
            if not isinstance(payload, dict):
                payload = {}
            if kind == "call":
                call_id = payload.get("call_id")
                name = payload.get("name")
                if str(name) == SESSION_TODO_TOOL_NAME:
                    # Каждый вызов — развёрнутым чек-листом (v3 спеки: правило
                    # «первый/смена состава» упразднено живой доводкой).
                    # Битые аргументы (items is None) — НЕ чек-лист: проваливаемся
                    # в generic tool-строку, статус сбоя виден механикой.
                    items = todo_items_from_arguments(payload.get("arguments"))
                    if items is not None:
                        actions.append(
                            SidecarNestedAction(
                                kind="mount_nested_todo",
                                parent_call_id=pid,
                                tool_call_id=str(call_id) if call_id else "",
                                todo_items=items,
                            )
                        )
                        continue
                if not nested_call_row(str(name or "")):
                    continue
                actions.append(
                    SidecarNestedAction(
                        kind="mount_nested_tool",
                        parent_call_id=pid,
                        tool_name=str(name) if name else "",
                        tool_call_id=str(call_id) if call_id else "",
                        args_summary=tool_args_summary(
                            str(name) if name else None,
                            payload.get("arguments"),
                            root=root,
                            nested=True,
                        ),
                        command=run_command_text(
                            str(name) if name else None,
                            payload.get("arguments"),
                        ),
                    )
                )
            elif kind == "result":
                call_id = payload.get("call_id")
                actions.append(
                    SidecarNestedAction(
                        kind="finish_nested_tool",
                        parent_call_id=pid,
                        tool_call_id=str(call_id) if call_id else "",
                        ok=True,
                        output=_raw_output(payload),
                    )
                )
            elif kind == "error":
                call_id = payload.get("call_id")
                stop = result_stop(payload.get("output"))
                _ok, brief = parse_tool_result_status(payload.get("output"))
                if brief is None and isinstance(payload.get("output"), str):
                    brief = payload["output"].strip()[:120] or None
                actions.append(
                    SidecarNestedAction(
                        kind="finish_nested_tool",
                        parent_call_id=pid,
                        tool_call_id=str(call_id) if call_id else "",
                        ok=False,
                        error_brief=stop[1] if stop else brief,
                        outcome=stop[0] if stop else None,
                        output=_raw_output(payload),
                    )
                )
            elif kind == "intermediate_reply":
                prose = payload.get("prose")
                if isinstance(prose, str) and prose.strip():
                    brief = payload.get("error_brief")
                    actions.append(
                        SidecarNestedAction(
                            kind="mount_nested_prose",
                            parent_call_id=pid,
                            prose=prose.strip(),
                            ok=payload.get("ok") is not False,
                            error_brief=brief
                            if isinstance(brief, str)
                            else None,
                            tool_call_id=(
                                str(payload["call_id"])
                                if isinstance(payload.get("call_id"), str)
                                else None
                            ),
                        )
                    )
            elif kind == "user":
                content = payload.get("content")
                if isinstance(content, str) and content.strip():
                    actions.append(
                        SidecarNestedAction(
                            kind="mount_nested_user",
                            parent_call_id=pid,
                            prose=content.strip(),
                            images=tuple(attachments_of(payload)),
                        )
                    )
            elif kind == "child_summary":
                actions.append(
                    SidecarNestedAction(
                        kind="mount_nested_summary",
                        parent_call_id=pid,
                        prose=CHILD_SUMMARY_LABEL,
                    )
                )
            # finished / unknown → no UI row
    return actions


def apply_sidecar_nested_actions(
    actions: Sequence[SidecarNestedAction],
    *,
    mount_nested_tool: Callable[..., None],
    finish_nested_tool: Callable[..., None],
    mount_nested_prose: Callable[..., None],
    mount_nested_summary: Callable[..., None] | None = None,
    mount_nested_user: Callable[..., None] | None = None,
    mount_nested_todo: Callable[..., None] | None = None,
) -> None:
    for act in actions:
        if act.kind == "mount_nested_tool":
            mount_nested_tool(
                parent_call_id=act.parent_call_id,
                tool_name=act.tool_name or "",
                tool_call_id=act.tool_call_id or "",
                args_summary=act.args_summary or "",
                command=act.command,
            )
        elif act.kind == "finish_nested_tool":
            finish_nested_tool(
                parent_call_id=act.parent_call_id,
                tool_call_id=act.tool_call_id or "",
                ok=bool(act.ok) if act.ok is not None else True,
                error_brief=act.error_brief,
                outcome=act.outcome,
                output=act.output,
            )
        elif act.kind == "mount_nested_prose":
            mount_nested_prose(
                parent_call_id=act.parent_call_id,
                prose=act.prose or "",
                ok=act.ok is not False,
                error_brief=act.error_brief,
            )
        elif act.kind == "mount_nested_user":
            if mount_nested_user is not None:
                mount_nested_user(
                    parent_call_id=act.parent_call_id,
                    prose=act.prose or "",
                    images=act.images,
                )
            else:
                mount_nested_prose(
                    parent_call_id=act.parent_call_id,
                    prose=act.prose or "",
                )
        elif act.kind == "mount_nested_summary":
            if mount_nested_summary is not None:
                mount_nested_summary(
                    parent_call_id=act.parent_call_id,
                    prose=act.prose or CHILD_SUMMARY_LABEL,
                )
            else:
                mount_nested_prose(
                    parent_call_id=act.parent_call_id,
                    prose=act.prose or CHILD_SUMMARY_LABEL,
                )
        elif act.kind == "mount_nested_todo":
            if mount_nested_todo is not None:
                mount_nested_todo(
                    parent_call_id=act.parent_call_id,
                    tool_call_id=act.tool_call_id or "",
                    todo_items=act.todo_items or [],
                )
            else:
                # потребитель без todo-колбэка — деградация до свёрнутой строки
                mount_nested_tool(
                    parent_call_id=act.parent_call_id,
                    tool_name="session_todo",
                    tool_call_id=act.tool_call_id or "",
                    args_summary=tool_args_summary(
                        "session_todo", {"todos": act.todo_items or []}
                    ),
                )


def detail_from_sidecar_records(
    records: Sequence[Dict[str, Any]], call_id: str
) -> Optional[Dict[str, Any]]:
    """Собрать call/result pair по child call_id из sidecar records."""
    if not call_id:
        return None
    call_payload: Optional[Dict[str, Any]] = None
    result_payload: Optional[Dict[str, Any]] = None
    result_kind: Optional[str] = None
    for rec in records:
        if not isinstance(rec, dict):
            continue
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("call_id") != call_id:
            continue
        kind = rec.get("kind")
        if kind == "call":
            call_payload = payload
        elif kind in ("result", "error"):
            result_payload = payload
            result_kind = kind
    if call_payload is None:
        return None
    out: Dict[str, Any] = {"call": call_payload}
    if result_payload is not None:
        out["result"] = result_payload
        out["result_kind"] = result_kind or "result"
    return out


def nested_result_event(
    call_id: str,
    *,
    ok: bool,
    error_brief: Optional[str],
    outcome: Optional[ResultOutcome],
    output: Optional[str],
) -> TraceEvent:
    """Исход вложенного вызова событием ленты (спека P4 §7): сводка и
    вывод команды — из сырого вывода тем же маппером, что у верхнего
    уровня; стоп, лимит и отказ — причина из фактов исхода. Один билдер
    для живой ленты, возврата в сессию и превью."""
    if outcome is None and output is not None:
        summary = output_summary(output, error=not ok)
    else:
        summary = "" if ok else (error_brief or "error")
    return TraceEvent(
        no=0,
        event_ts_utc_iso="",
        kind="result" if ok else "error",
        title="",
        summary=summary,
        full_payload={},
        call_id=call_id,
        outcome=outcome or ("ok" if ok else "error"),
        command=parse_command_output(output),
    )


def nested_raw_texts(
    records: Sequence[Dict[str, Any]], call_id: str
) -> Tuple[Optional[str], Optional[str]]:
    """Сырой текст команды Run-тула и сырой вывод вложенного вызова из
    записей sidecar — когда событие прогресса их не донесло (сверх
    потолка); записи нет — `(None, None)`."""
    detail = detail_from_sidecar_records(records, call_id)
    if detail is None:
        return None, None
    call = detail["call"]
    name = call.get("name")
    command = run_command_text(
        str(name) if name else None, call.get("arguments")
    )
    result = detail.get("result")
    output = _raw_output(result) if isinstance(result, dict) else None
    return command, output


def resolve_nested_tool_detail(
    call_id: str, *, sidecar_records: Sequence[Dict[str, Any]] | None = None
) -> Optional[Dict[str, Any]]:
    """Detail из sidecar records на диске (mid-session и reopen)."""
    if not call_id or sidecar_records is None:
        return None
    return detail_from_sidecar_records(sidecar_records, call_id)


def trace_events_from_nested_detail(
    call_id: str, detail: Dict[str, Any], *, root: Optional[str] = None
) -> Tuple[TraceEvent, Optional[TraceEvent]]:
    """Синтетические TraceEvent для ToolCallDetailScreen."""
    call_payload = detail.get("call")
    if not isinstance(call_payload, dict):
        raise ValueError("detail.call required")
    name = str(call_payload.get("name") or "")
    args_summary = tool_args_summary(
        name or None, call_payload.get("arguments"), root=root, nested=True
    )
    call_ev = TraceEvent(
        no=0,
        event_ts_utc_iso="",
        kind="call",
        title=name,
        summary=args_summary,
        args_summary=args_summary,
        full_payload=call_payload,
        call_id=call_id,
        tool_name=name,
        wire_name=name or None,
    )
    result_ev: Optional[TraceEvent] = None
    result_payload = detail.get("result")
    if isinstance(result_payload, dict):
        rk = detail.get("result_kind") or "result"
        result_ev = TraceEvent(
            no=0,
            event_ts_utc_iso="",
            kind="error" if rk == "error" else "result",
            title="",
            summary="",
            full_payload=result_payload,
            call_id=call_id,
        )
    return call_ev, result_ev


__all__ = [
    "CHILD_SUMMARY_LABEL",
    "SidecarNestedAction",
    "apply_sidecar_nested_actions",
    "detail_from_sidecar_records",
    "nested_raw_texts",
    "nested_result_event",
    "plan_sidecar_nested_actions",
    "resolve_nested_tool_detail",
    "trace_events_from_nested_detail",
]
