"""Synchronous file maintenance using the real ThreadSession batch policy."""

from __future__ import annotations

from gc_core.thread_store import ThreadSession, sanitize_system_id
from gc_core.thread_store_file import FileThreadStore
from gc_core.thread_store_journal import thread_read_scope


def append_file_session_messages(
    threads_dir, system_id, thread_id, messages, *, config_dict=None
):
    """Append an idle UI's messages and metadata atomically, without an agent loop.

    This narrow synchronous entry point reuses ThreadSession's preparation and
    receipt validation. It does not acquire execution ownership on behalf of UI.
    """
    store = FileThreadStore(threads_dir)
    system_id = sanitize_system_id(system_id)
    with thread_read_scope(threads_dir, system_id, thread_id):
        snapshot = store.read_snapshot(system_id, thread_id)
        ThreadSession._check_store_owner(store, snapshot.meta, snapshot)
        session = ThreadSession._build(
            config_dict=config_dict or {},
            threads_dir=threads_dir,
            store=store,
            sys_norm=system_id,
            thread_id=thread_id,
            meta=snapshot.meta,
            state=snapshot.state,
        )
        session._store_snapshot = snapshot
        staged, operation = session._prepare_durable_persist(
            list(messages), session.previous_response_id
        )
        receipt = store.commit_operation(system_id, thread_id, operation)
        session._apply_durable_persist(staged, operation, receipt)
        return receipt.written
