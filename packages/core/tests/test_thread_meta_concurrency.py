"""Мета треда при параллельных писателях (хранилище v2, файловое).

TUI пишет свою секцию (``frontend["tui"]``) из своего потока в любой
момент, исполнитель — ``runtime`` и шаги хода. Читатель не видит
обрезанного файла, писатель не падает на чужом временном файле, ключи
другого писателя не теряются — в том числе когда сессия пишет шаг или
сжатие по снимку, прочитанному до записи TUI.
"""

import threading

from gc_core.thread_store import ThreadSession
from gc_core.threads.facade import Threads, run_sync
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import RuntimeStatus, SideChange, ThreadKey
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="cli", thread="t")


def _tui_write(store, i):
    run_sync(Threads(store).update_frontend(KEY, "tui", {"status": f"x{i}"}))


def _runtime_write(store, i):
    change = SideChange(
        op_id=f"rt{i}", set_runtime=RuntimeStatus(live_history_chars=i)
    )
    run_sync(store.append_side(KEY, change))


def _race(store, seconds=1.0):
    errors = []
    stop = threading.Event()

    def loop(fn):
        i = 0
        while not stop.is_set():
            try:
                fn(store, i)
            except Exception as exc:
                errors.append(type(exc).__name__)
            i += 1

    def reader(store, _i):
        run_sync(store.get_thread(KEY))

    threads = [
        threading.Thread(target=loop, args=(fn,))
        for fn in (_tui_write, _runtime_write, reader)
    ]
    for t in threads:
        t.start()
    stop.wait(seconds)
    stop.set()
    for t in threads:
        t.join()
    return sorted(set(errors))


def test_concurrent_writers_never_expose_a_broken_meta(tmp_path):
    store = FileThreadStore(str(tmp_path))
    _tui_write(store, 0)

    assert _race(store) == []


def test_concurrent_writers_keep_each_others_keys(tmp_path):
    store = FileThreadStore(str(tmp_path))
    _tui_write(store, 0)

    _race(store)
    record = run_sync(store.get_thread(KEY))

    assert record.frontend["tui"]["status"].startswith("x")
    assert record.runtime.live_history_chars is not None


def test_discard_ignores_a_stale_fixed_tmp_next_to_data(tmp_path):
    store = FileThreadStore(str(tmp_path))
    message = {"id": "m1", "role": "user", "content": "a"}
    run_sync(Threads(store).append_messages(KEY, [message]))
    (tmp_path / "data_cli_t.jsonl.tmp").mkdir()

    assert run_sync(Threads(store).discard_message(KEY, "m1"))
    assert run_sync(store.read_stream(KEY, "messages")) == []


async def _session(store):
    return await ThreadSession.afrom_config(
        settings=make_settings({}),
        system_id="cli",
        thread_id="t",
        store=store,
    )


def _user(mid):
    return {"id": mid, "role": "user", "content": f"вопрос {mid}"}


async def test_step_keeps_tui_key_written_after_session_read(tmp_path):
    """Сессия прочитала тред до записи TUI: её шаг секцию TUI не затирает."""
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    await Threads(store).update_frontend(KEY, "tui", {"copy_buffer": "x"})

    await session.apersist_batch([_user("u1")], previous_response_id=None)

    record = await store.get_thread(KEY)
    assert record.frontend["tui"]["copy_buffer"] == "x"
    assert record.info.messages_count == 1


async def test_compaction_keeps_tui_key_written_after_session_read(tmp_path):
    store = FileThreadStore(str(tmp_path))
    session = await _session(store)
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    await Threads(store).update_frontend(KEY, "tui", {"copy_buffer": "x"})

    await session.aarchive_and_compact_summary_payload(
        {"summary_message_content": "сводка", "dialog_steps_offset": 1}
    )

    record = await store.get_thread(KEY)
    assert record.frontend["tui"]["copy_buffer"] == "x"
    assert record.info.messages_count == 1
