"""Map persisted data_subagents_* records → nested TUI mount actions.

Display/audit only — does not touch agent state or parent LLM history.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence

from tools.subagents.progress import brief_args_summary, parse_tool_result_status

CHILD_SUMMARY_LABEL = "контекст субагента сжат"

SidecarActionKind = Literal[
    "mount_nested_tool",
    "finish_nested_tool",
    "mount_nested_prose",
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
        elif act.kind == "mount_nested_summary":
            mount_nested_summary(
                parent_call_id=act.parent_call_id,
                prose=act.prose or CHILD_SUMMARY_LABEL,
            )


__all__ = [
    "CHILD_SUMMARY_LABEL",
    "SidecarNestedAction",
    "apply_sidecar_nested_actions",
    "plan_sidecar_nested_actions",
]
