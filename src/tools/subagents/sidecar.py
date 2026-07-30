"""Sidecar nested-trace: gate and append writer.

Persist is independent of the live progress sink (CLI / no-op sink still writes
when thread identity is present). Nested tool detail for TUI Ctrl+click comes
from data_subagents_*.jsonl on disk — no in-memory detail cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from thread_store import (
    append_subagent_trace_record,
    sanitize_system_id,
    validate_thread_id,
)


@dataclass(frozen=True)
class SidecarIdentity:
    threads_dir: str
    system_id: str
    thread_id: str
    parent_call_id: str
    child_run_id: str


def bind_thread_identity(
    ctx: Any, *, threads_dir: str, system_id: str, thread_id: str
) -> None:
    """Записать thread identity на ctx (parent run с тредом)."""
    ctx.threads_dir = threads_dir
    ctx.system_id = system_id
    ctx.thread_id = thread_id


def clear_thread_identity(ctx: Any) -> None:
    """Снять identity (run без треда / смена сессии)."""
    ctx.threads_dir = None
    ctx.system_id = None
    ctx.thread_id = None


def resolve_sidecar_identity(ctx: Any) -> Optional[SidecarIdentity]:
    """Gate sidecar: parent_call_id + валидный thread path. Sink не требуется."""
    parent_call_id = getattr(ctx, "parent_call_id", None)
    if not isinstance(parent_call_id, str) or not parent_call_id.strip():
        return None

    threads_dir = getattr(ctx, "threads_dir", None)
    system_id = getattr(ctx, "system_id", None)
    thread_id = getattr(ctx, "thread_id", None)
    if not isinstance(threads_dir, str) or not threads_dir.strip():
        return None
    if not isinstance(system_id, str) or not system_id.strip():
        return None
    if not isinstance(thread_id, str) or not thread_id.strip():
        return None

    try:
        sys_norm = sanitize_system_id(system_id)
        tid = validate_thread_id(thread_id)
    except ValueError:
        return None

    child_run_id = getattr(ctx, "run_id", None)
    if not isinstance(child_run_id, str):
        child_run_id = ""

    return SidecarIdentity(
        threads_dir=threads_dir.strip(),
        system_id=sys_norm,
        thread_id=tid,
        parent_call_id=parent_call_id.strip(),
        child_run_id=child_run_id,
    )


def persist_subagent_trace(
    ctx: Any, *, kind: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Append sidecar record when gate passes. Else no-op."""
    ident = resolve_sidecar_identity(ctx)
    if ident is None:
        return None
    return append_subagent_trace_record(
        ident.threads_dir,
        ident.system_id,
        ident.thread_id,
        parent_call_id=ident.parent_call_id,
        child_run_id=ident.child_run_id,
        kind=kind,
        payload=payload,
    )


__all__ = [
    "SidecarIdentity",
    "bind_thread_identity",
    "clear_thread_identity",
    "persist_subagent_trace",
    "resolve_sidecar_identity",
]
