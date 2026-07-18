"""Map persisted data_subagents_* records → nested TUI mount actions / detail.

Display/audit only — does not touch agent state or parent LLM history.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple

from tools.subagents.progress import brief_args_summary, parse_tool_result_status
from tools.subagents.sidecar import lookup_tool_detail
from tui.models import TraceEvent

CHILD_SUMMARY_LABEL = "контекст субагента сжат"

SidecarActionKind = Literal[
    "mount_nested_tool",
    "finish_nested_tool",
    "mount_nested_prose",
    "mount_nested_user",
    "mount_nested_summary",
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


def _seq_key(record: Dict[str, Any]) -> int:
    seq = record.get("seq")
    return seq if isinstance(seq, int) else 0


def plan_sidecar_nested_actions(
    records: Sequence[Dict[str, Any]],
) -> List[SidecarNestedAction]:
    """Group by parent_call_id, order by seq; skip finished/HITL-like noise."""
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
                actions.append(
                    SidecarNestedAction(
                        kind="mount_nested_tool",
                        parent_call_id=pid,
                        tool_name=str(name) if name else "",
                        tool_call_id=str(call_id) if call_id else "",
                        args_summary=brief_args_summary(payload.get("arguments")),
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
                    )
                )
            elif kind == "error":
                call_id = payload.get("call_id")
                _ok, brief = parse_tool_result_status(payload.get("output"))
                if brief is None and isinstance(payload.get("output"), str):
                    brief = payload["output"].strip()[:120] or None
                actions.append(
                    SidecarNestedAction(
                        kind="finish_nested_tool",
                        parent_call_id=pid,
                        tool_call_id=str(call_id) if call_id else "",
                        ok=False,
                        error_brief=brief,
                    )
                )
            elif kind == "intermediate_reply":
                prose = payload.get("prose")
                if isinstance(prose, str) and prose.strip():
                    actions.append(
                        SidecarNestedAction(
                            kind="mount_nested_prose",
                            parent_call_id=pid,
                            prose=prose.strip(),
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
    mount_nested_summary: Callable[..., None],
    mount_nested_user: Callable[..., None] | None = None,
) -> None:
    for act in actions:
        if act.kind == "mount_nested_tool":
            mount_nested_tool(
                parent_call_id=act.parent_call_id,
                tool_name=act.tool_name or "",
                tool_call_id=act.tool_call_id or "",
                args_summary=act.args_summary or "",
            )
        elif act.kind == "finish_nested_tool":
            finish_nested_tool(
                parent_call_id=act.parent_call_id,
                tool_call_id=act.tool_call_id or "",
                ok=bool(act.ok) if act.ok is not None else True,
                error_brief=act.error_brief,
            )
        elif act.kind == "mount_nested_prose":
            mount_nested_prose(
                parent_call_id=act.parent_call_id,
                prose=act.prose or "",
            )
        elif act.kind == "mount_nested_user":
            if mount_nested_user is not None:
                mount_nested_user(
                    parent_call_id=act.parent_call_id,
                    prose=act.prose or "",
                )
            else:
                mount_nested_prose(
                    parent_call_id=act.parent_call_id,
                    prose=act.prose or "",
                )
        elif act.kind == "mount_nested_summary":
            mount_nested_summary(
                parent_call_id=act.parent_call_id,
                prose=act.prose or CHILD_SUMMARY_LABEL,
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


def resolve_nested_tool_detail(
    call_id: str, *, sidecar_records: Sequence[Dict[str, Any]] | None = None
) -> Optional[Dict[str, Any]]:
    """cache (mid-session) → sidecar records (reopen)."""
    cached = lookup_tool_detail(call_id)
    if cached and isinstance(cached.get("call"), dict):
        return cached
    if sidecar_records is not None:
        return detail_from_sidecar_records(sidecar_records, call_id)
    return None


def trace_events_from_nested_detail(
    call_id: str, detail: Dict[str, Any]
) -> Tuple[TraceEvent, Optional[TraceEvent]]:
    """Синтетические TraceEvent для ToolCallDetailScreen."""
    call_payload = detail.get("call")
    if not isinstance(call_payload, dict):
        raise ValueError("detail.call required")
    name = str(call_payload.get("name") or "")
    call_ev = TraceEvent(
        no=0,
        event_ts_utc_iso="",
        kind="call",
        title=name,
        summary=brief_args_summary(call_payload.get("arguments")),
        full_payload=call_payload,
        call_id=call_id,
        tool_name=name,
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
    "plan_sidecar_nested_actions",
    "resolve_nested_tool_detail",
    "trace_events_from_nested_detail",
]
