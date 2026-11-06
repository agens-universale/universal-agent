"""TUI consumer for nested subagent progress + batch waiting/active phases.

Runtime кладёт события в QueueSubagentProgressSink (worker-поток).
Этот модуль — только UI: apply действий, без оркестрации execute.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Callable, Optional, Sequence

from gc_core.attachments.model import attachments_of
from gc_core.constants.tool_display import DisplayCategory, McpToolInfo
from gc_core.constants.tool_names import SESSION_TODO_TOOL_NAME
from gc_core.execution.progress import SubagentProgressEvent

from gc_tui.models import ResultOutcome
from gc_tui.services.subagent_sidecar_mapper import nested_call_row
from gc_tui.services.trace_mapper import classify_stop
from gc_tui.services.trace_render import tool_caption


def _finish_facts(
    ev: SubagentProgressEvent,
) -> tuple[bool, Optional[str], Optional[ResultOutcome]]:
    """Итог вложенного вызова из `tool_end`: ``(ok, сводка, исход)``.

    Стоп, лимит и отказ — тем же классификатором, что у перерисовки из
    записи субагента; событие без фактов исхода — ошибка, как раньше."""
    ok = ev.ok if ev.ok is not None else True
    stop = None if ok else classify_stop(ev.result_status, ev.result_error)
    if stop is None:
        return ok, ev.error_brief, None
    return ok, stop[1], stop[0]


def first_pending(rows: Sequence[Any]) -> Optional[Any]:
    """Батч исполняется по порядку: первая незавершённая строка — та, что
    стартовала; остальные ждут. Одинаково для любых тулов. ``rows`` — в
    порядке появления в батче (порядок вставки `_active_calls`)."""
    for row in rows:
        if getattr(row, "_result", None) is None and not getattr(
            row, "_interrupted", False
        ):
            return row
    return None


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
                if not routed_todo and nested_call_row(ev.tool_name or ""):
                    mount_nested_tool(
                        parent_call_id=pid,
                        tool_name=ev.tool_name or "<tool>",
                        tool_call_id=ev.tool_call_id or "",
                        args_summary=ev.args_summary or "",
                        step=ev.step,
                        max_steps=ev.max_steps,
                        command=ev.command,
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
                ok, error_brief, outcome = _finish_facts(ev)
                finish_nested_tool(
                    parent_call_id=pid,
                    tool_name=ev.tool_name,
                    tool_call_id=ev.tool_call_id or "",
                    ok=ok,
                    error_brief=error_brief,
                    outcome=outcome,
                    step=ev.step,
                    max_steps=ev.max_steps,
                    output=ev.output,
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
                    ok=ev.ok is not False,
                    error_brief=ev.error_brief,
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
    *,
    step: int,
    max_steps: int,
    last_tool: Optional[str],
    mcp_tools: Optional[Mapping[str, McpToolInfo]] = None,
    mcp_servers: Iterable[str] = (),
) -> str:
    """Хвост корневой строки субагента: `step 4/100 · last=Read`.

    `last` — имя тула, как в строке вызова (спека P3 §2.1): подпись ядра,
    MCP — `сервер · тул`, неизвестный — wire-имя; скрытый — без `last`."""
    parts = [f"step {step}/{max_steps}" if max_steps else f"step {step}"]
    if last_tool:
        category, name = tool_caption(last_tool, mcp_tools, mcp_servers)
        if category is not DisplayCategory.HIDDEN:
            parts.append(f"last={name}")
    return " · ".join(parts)
