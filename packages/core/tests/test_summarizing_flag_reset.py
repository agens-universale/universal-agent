"""areset_stale_summarizing_flag: протухший runtime.summarizing снимается на старте run'а."""

from gc_core.run_summary_coordinator import areset_stale_summarizing_flag
from gc_core.thread_store import load_thread_meta, save_thread_meta


class _StubSession:
    """Похожа на ThreadSession — с полем ``store``.

    Флаг ``summarizing`` пишется через хранилище сессии, а не через модульную
    функцию: иначе у встраивающего с базой он уходил бы на диск мимо всего
    остального треда.
    """

    def __init__(self, threads_dir, meta):
        from gc_core.thread_store_file import FileThreadStore

        self.threads_dir = threads_dir
        self.system_id = "cli"
        self.thread_id = "t1"
        self.meta = meta
        self.store = FileThreadStore(str(threads_dir))
        # Асинхронная форма ходит через ПОРТ, как настоящая ThreadSession.
        from gc_core.thread_store_async import athread_store

        self.port = athread_store(self.store)


async def test_reset_clears_stale_flag_on_disk_and_in_memory(tmp_path):
    save_thread_meta(
        str(tmp_path),
        "cli",
        "t1",
        {"runtime": {"summarizing": True}, "messages_count": 3},
    )
    ts = _StubSession(
        str(tmp_path),
        {"runtime": {"summarizing": True}, "messages_count": 3},
    )

    await areset_stale_summarizing_flag(ts)

    assert not (ts.meta.get("runtime") or {}).get("summarizing")
    disk = load_thread_meta(str(tmp_path), "cli", "t1")
    assert not (disk.get("runtime") or {}).get("summarizing")
    assert disk.get("messages_count") == 3


async def test_reset_noop_when_flag_absent(tmp_path):
    save_thread_meta(str(tmp_path), "cli", "t1", {"messages_count": 1})
    ts = _StubSession(str(tmp_path), {"messages_count": 1})
    await areset_stale_summarizing_flag(ts)
    assert load_thread_meta(str(tmp_path), "cli", "t1").get("messages_count") == 1


async def test_reset_none_session_is_noop():
    await areset_stale_summarizing_flag(None)
