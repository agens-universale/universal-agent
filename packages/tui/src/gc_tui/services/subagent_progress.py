"""TUI consumer for nested subagent progress + batch waiting/active phases.

Runtime кладёт события в QueueSubagentProgressSink (worker-поток).
Этот модуль — только UI: apply действий, без оркестрации execute.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from gc_core.attachments.model import attachments_of
from gc_core.constants.tool_display import tool_display
from gc_core.constants.tool_names import SESSION_TODO_TOOL_NAME
from gc_core.execution.progress import SubagentProgressEvent


def reconcile_batch_phases(rows: Sequence[Any]) -> None:
    """Среди незавершённых строк batch: первая → active, остальные → waiting.

    Одинаково для любых тулов (не special-case subagent). rows — в порядке
    появления в batch (insertion order _active_calls).
    """
    pending = [
        r
        for r in rows
        if getattr(r, "_result", None) is None
        and not getattr(r, "_interrupted", False)
    ]
    if not pending:
        return
    first, *rest = pending
    set_active = getattr(first, "set_active", None)
    if callable(set_active):
        set_active()
    for row in rest:
        set_waiting = getattr(row, "set_waiting", None)
        if callable(set_waiting):
            set_waiting()


class SubagentProgressConsumer:
    """Stateful consumer: last_tool / seen call_ids между drain'ами."""

    def __init__(self) -> None:
        self._last_tool: dict[str, Optional[str]] = {}
        self._seen_tool_calls: set[str] = set()

    def apply_drained(
        self,
        events: Sequence[SubagentProgressEvent],
        *,
        mount_nested_tool: Callable[..., None],
        finish_nested_tool: Callable[..., None],
        mount_nested_prose: Callable[..., None],
        parent_heartbeat: Callable[..., None],
        show_ask_human: Callable[..., None] | None = None,
        hide_ask_human: Callable[..., None] | None = None,
        mount_nested_user: Callable[..., None] | None = None,
        mount_nested_todo: Callable[..., None] | None = None,
    ) -> None:
        for ev in events:
            pid = ev.parent_call_id
            if ev.kind == "tool_start":
                if ev.tool_name:
                    self._last_tool[pid] = ev.tool_name
                if ev.tool_call_id:
                    self._seen_tool_calls.add(ev.tool_call_id)
                routed_todo = False
                # Каждый вызов session_todo — развёрнутым чек-листом (v3 спеки:
                # правило «первый/смена состава» упразднено живой доводкой).
                if (
                    ev.tool_name == SESSION_TODO_TOOL_NAME
                    and ev.todo_items is not None
                    and mount_nested_todo is not None
                ):
                    mount_nested_todo(
                        parent_call_id=pid,
                        tool_call_id=ev.tool_call_id or "",
                        todo_items=ev.todo_items,
                    )
                    routed_todo = True
                if not routed_todo:
                    mount_nested_tool(
                        parent_call_id=pid,
                        tool_name=ev.tool_name or "<tool>",
                        tool_call_id=ev.tool_call_id or "",
                        args_summary=ev.args_summary or "",
                        step=ev.step,
                        max_steps=ev.max_steps,
                    )
                parent_heartbeat(
                    parent_call_id=pid,
                    step=ev.step,
                    max_steps=ev.max_steps,
                    last_tool=self._last_tool.get(pid),
                )
            elif ev.kind == "tool_end":
                if ev.tool_name:
                    self._last_tool[pid] = ev.tool_name
                finish_nested_tool(
                    parent_call_id=pid,
                    tool_name=ev.tool_name,
                    tool_call_id=ev.tool_call_id or "",
                    ok=ev.ok if ev.ok is not None else True,
                    error_brief=ev.error_brief,
                    step=ev.step,
                    max_steps=ev.max_steps,
                )
                parent_heartbeat(
                    parent_call_id=pid,
                    step=ev.step,
                    max_steps=ev.max_steps,
                    last_tool=self._last_tool.get(pid),
                )
            elif ev.kind == "intermediate_reply":
                mount_nested_prose(
                    parent_call_id=pid,
                    prose=ev.prose or "",
                    tool_name=ev.tool_name,
                    step=ev.step,
                    max_steps=ev.max_steps,
                )
                parent_heartbeat(
                    parent_call_id=pid,
                    step=ev.step,
                    max_steps=ev.max_steps,
                    last_tool=self._last_tool.get(pid),
                )
            elif ev.kind == "user_message":
                if mount_nested_user is not None:
                    mount_nested_user(
                        parent_call_id=pid,
                        prose=ev.prose or "",
                        step=ev.step,
                        max_steps=ev.max_steps,
                        images=tuple(
                            attachments_of({"attachments": ev.attachments})
                        ),
                    )
                else:
                    mount_nested_prose(
                        parent_call_id=pid,
                        prose=ev.prose or "",
                        step=ev.step,
                        max_steps=ev.max_steps,
                    )
                parent_heartbeat(
                    parent_call_id=pid,
                    step=ev.step,
                    max_steps=ev.max_steps,
                    last_tool=self._last_tool.get(pid),
                )
            elif ev.kind == "ask_human":
                if show_ask_human is not None:
                    show_ask_human(
                        parent_call_id=pid,
                        prose=ev.prose or "",
                        via=ev.tool_name,
                        tool_call_id=ev.tool_call_id,
                    )
            elif ev.kind == "ask_human_done":
                if hide_ask_human is not None:
                    hide_ask_human(parent_call_id=pid)
            elif ev.kind in (
                "confirm",
                "confirm_done",
                "started",
                "heartbeat",
                "usage",
                "finished",
                "error",
                "cancelled",
            ):
                parent_heartbeat(
                    parent_call_id=pid,
                    step=ev.step,
                    max_steps=ev.max_steps,
                    last_tool=self._last_tool.get(pid),
                )
                if ev.kind in ("finished", "error", "cancelled"):
                    self._last_tool.pop(pid, None)

    def reset(self) -> None:
        self._last_tool.clear()
        self._seen_tool_calls.clear()


def format_parent_live_status(
    *, step: int, max_steps: int, last_tool: Optional[str]
) -> str:
    """Хвост корневой строки субагента: `step 4/100 · last=Read`.

    `last` — подпись тула из ядра, не wire-имя (спека P3 §2.1)."""
    parts = [f"step {step}/{max_steps}" if max_steps else f"step {step}"]
    if last_tool:
        parts.append(f"last={tool_display(last_tool).label}")
    return " · ".join(parts)
