"""TUI consumer for nested subagent progress + batch waiting/active phases.

Runtime кладёт события в QueueSubagentProgressSink (worker-поток).
Этот модуль — только UI: plan/apply действий, без оркестрации execute.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Literal, Optional, Sequence

from gc_core.constants.tool_names import SESSION_TODO_TOOL_NAME
from gc_core.tools.subagents.progress import SubagentProgressEvent

ProgressActionKind = Literal[
    "mount_nested_tool",
    "finish_nested_tool",
    "mount_nested_prose",
    "mount_nested_user",
    "parent_heartbeat",
    "show_ask_human",
    "hide_ask_human",
]


@dataclass(frozen=True)
class ProgressAction:
    kind: ProgressActionKind
    parent_call_id: str
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    args_summary: Optional[str] = None
    prose: Optional[str] = None
    ok: Optional[bool] = None
    error_brief: Optional[str] = None
    step: int = 0
    max_steps: int = 0
    last_tool: Optional[str] = None
    via: Optional[str] = None


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


def plan_progress_actions(
    events: Sequence[SubagentProgressEvent],
) -> List[ProgressAction]:
    """Превратить drained events в список UI-действий (без виджетов).

    Test-only: не покрывает развёрнутый путь session_todo (routed_todo) —
    продовый путь для него SubagentProgressConsumer.apply_drained ниже."""
    actions: List[ProgressAction] = []
    last_tool: dict[str, Optional[str]] = {}

    def _heartbeat(ev: SubagentProgressEvent) -> ProgressAction:
        return ProgressAction(
            kind="parent_heartbeat",
            parent_call_id=ev.parent_call_id,
            step=ev.step,
            max_steps=ev.max_steps,
            last_tool=last_tool.get(ev.parent_call_id),
            tool_name=ev.tool_name,
        )

    for ev in events:
        pid = ev.parent_call_id
        if ev.kind == "tool_start":
            if ev.tool_name:
                last_tool[pid] = ev.tool_name
            actions.append(
                ProgressAction(
                    kind="mount_nested_tool",
                    parent_call_id=pid,
                    tool_name=ev.tool_name,
                    tool_call_id=ev.tool_call_id,
                    args_summary=ev.args_summary or "",
                    step=ev.step,
                    max_steps=ev.max_steps,
                )
            )
            actions.append(_heartbeat(ev))
        elif ev.kind == "tool_end":
            if ev.tool_name:
                last_tool[pid] = ev.tool_name
            actions.append(
                ProgressAction(
                    kind="finish_nested_tool",
                    parent_call_id=pid,
                    tool_name=ev.tool_name,
                    tool_call_id=ev.tool_call_id,
                    ok=ev.ok if ev.ok is not None else True,
                    error_brief=ev.error_brief,
                    step=ev.step,
                    max_steps=ev.max_steps,
                )
            )
            actions.append(_heartbeat(ev))
        elif ev.kind == "intermediate_reply":
            actions.append(
                ProgressAction(
                    kind="mount_nested_prose",
                    parent_call_id=pid,
                    tool_name=ev.tool_name,
                    prose=ev.prose or "",
                    step=ev.step,
                    max_steps=ev.max_steps,
                )
            )
            actions.append(_heartbeat(ev))
        elif ev.kind == "user_message":
            actions.append(
                ProgressAction(
                    kind="mount_nested_user",
                    parent_call_id=pid,
                    prose=ev.prose or "",
                    step=ev.step,
                    max_steps=ev.max_steps,
                )
            )
            actions.append(_heartbeat(ev))
        elif ev.kind == "ask_human":
            actions.append(
                ProgressAction(
                    kind="show_ask_human",
                    parent_call_id=pid,
                    prose=ev.prose or "",
                    via=ev.tool_name,
                    tool_call_id=ev.tool_call_id,
                    step=ev.step,
                    max_steps=ev.max_steps,
                )
            )
        elif ev.kind == "ask_human_done":
            actions.append(
                ProgressAction(
                    kind="hide_ask_human",
                    parent_call_id=pid,
                    via=ev.tool_name,
                    ok=ev.ok,
                )
            )
        elif ev.kind in ("confirm", "confirm_done"):
            # Баннер confirm уже открывает tui_confirm(via=…); здесь только heartbeat.
            actions.append(_heartbeat(ev))
        elif ev.kind in (
            "finished",
            "error",
            "cancelled",
            "started",
            "heartbeat",
            "usage",
        ):
            # Nested в живой сессии оставляем под parent (✓/✗); collapse — только
            # при clear/смене треда.
            actions.append(_heartbeat(ev))
    return actions


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
    """Хвост корневой строки субагента: `step 4/100 · last=fs_read_file`."""
    parts = [f"step {step}/{max_steps}" if max_steps else f"step {step}"]
    if last_tool:
        parts.append(f"last={last_tool}")
    return " · ".join(parts)
