"""Sidecar nested-trace: gate and append writer.

Persist is independent of the live progress sink (CLI / no-op sink still writes
when thread identity is present). Nested tool detail for TUI Ctrl+click comes
from data_subagents_*.jsonl on disk — no in-memory detail cache.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from gc_core.thread_store import sanitize_system_id, validate_thread_id


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
    """Gate sidecar: parent_call_id + адрес треда. Sink не требуется.

    ``threads_dir`` в гейт НЕ входит, и это правка по факту: у стороннего
    хранилища (база, память) каталога нет вовсе, там пустая строка. Гейт её
    отвергал, identity не собиралась, и ВСЕ семь вызовов записи трассы
    молча возвращали ``None`` — трасса субагентов не писалась в нефайловое
    хранилище никогда. Тесты этого не видели, потому что передают
    ``threads_dir=str(tmp_path)`` явно.

    Адрес треда — это ``(system_id, thread_id)``. Каталог нужен только
    файловому запасному пути, и требуется он там, а не здесь.
    """
    parent_call_id = getattr(ctx, "parent_call_id", None)
    if not isinstance(parent_call_id, str) or not parent_call_id.strip():
        return None

    threads_dir = getattr(ctx, "threads_dir", None)
    if not isinstance(threads_dir, str):
        threads_dir = ""
    system_id = getattr(ctx, "system_id", None)
    thread_id = getattr(ctx, "thread_id", None)
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
        threads_dir=threads_dir.strip(),  # "" у хранилища без файлов — законно
        system_id=sys_norm,
        thread_id=tid,
        parent_call_id=parent_call_id.strip(),
        child_run_id=child_run_id,
    )


async def apersist_subagent_trace(
    ctx: Any, *, kind: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Дописать sidecar-запись, если гейт пропустил. Иначе no-op.

    ЕДИНСТВЕННАЯ форма. Синхронный близнец снят: он умел писать только в
    синхронное хранилище, а на асинхронном честно отказывался (варнинг и
    ``None``) — то есть у встраивающего с redis/postgres трасса субагентов не
    писалась ни разу, при полностью работающих субагентах.

    Самый тихий шов контракта: возврат отбрасывают ВСЕ вызывающие
    (``processing_responses`` ×4, ``subagents/runner`` ×3). Единственный
    вызывающий без своего loop — колбэк ``ChildInputScope.enqueue`` из чужого
    потока; он уводит эту корутину на loop прогона через
    ``run_coroutine_threadsafe`` (см. ``SubagentRunner``).
    """
    ident = resolve_sidecar_identity(ctx)
    if ident is None:
        return None
    store = getattr(ctx, "thread_store", None)
    if store is None:
        if not ident.threads_dir:
            # Ни хранилища, ни каталога — писать некуда, и молчать здесь
            # правильнее, чем писать куда попало: `FileThreadStore("")` уводит
            # запись в путь ОТНОСИТЕЛЬНО текущего каталога процесса. Ровно эта
            # молчаливая запись мимо контракта уже была однажды (см. докстроку
            # `FileThreadStore.archive_and_replace`).
            return None
        from gc_core.thread_store_file import FileThreadStore

        store = FileThreadStore(ident.threads_dir)

    from gc_core.thread_store_async import athread_store

    port = athread_store(store)
    return await port.aappend_subagent_trace(
        ident.system_id,
        ident.thread_id,
        parent_call_id=ident.parent_call_id,
        child_run_id=ident.child_run_id,
        kind=kind,
        payload=payload,
    )


__all__ = [
    "SidecarIdentity",
    "apersist_subagent_trace",
    "bind_thread_identity",
    "clear_thread_identity",
    "resolve_sidecar_identity",
]
