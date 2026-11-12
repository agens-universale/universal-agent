from gc_core.agent_executor import _aappend_trace_id_to_meta
from gc_core.thread_store import ThreadSession
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey

KEY = ThreadKey(system="cli", thread="t1")


async def _trace_ids(tmp_path):
    record = await FileThreadStore(tmp_path).get_thread(KEY)
    return record.runtime.trace_ids


async def _session(tmp_path):
    """Настоящая сессия: trace_id пишется её хранилищем и её арендой — у
    встраивающего с базой запись не уйдёт на диск мимо остального треда."""
    return await ThreadSession.afrom_config(
        settings=None,
        system_id=KEY.system,
        thread_id=KEY.thread,
        store=FileThreadStore(tmp_path),
    )


async def test_appends_first_trace(tmp_path):
    session = await _session(tmp_path)
    await _aappend_trace_id_to_meta(session, "wf-1")
    assert await _trace_ids(tmp_path) == ["wf-1"]


async def test_appends_in_order_and_dedups(tmp_path):
    session = await _session(tmp_path)
    await _aappend_trace_id_to_meta(session, "wf-1")
    await _aappend_trace_id_to_meta(session, "wf-2")
    await _aappend_trace_id_to_meta(session, "wf-1")
    assert await _trace_ids(tmp_path) == ["wf-1", "wf-2"]


async def test_missing_identity_is_a_noop(tmp_path):
    class _Bare:
        threads_dir = None
        system_id = None
        thread_id = None

    await _aappend_trace_id_to_meta(_Bare(), "wf-1")  # не должно бросать
