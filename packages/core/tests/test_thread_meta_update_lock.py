"""Мета сессии — чтение, слияние и запись под одним замком треда.

TUI пишет ``meta.tui`` из своего потока в любой момент; запись сессии
(шаг хода, сжатие) не должна затирать то, что TUI записал между её чтением
и записью.
"""

import gc
import threading

from gc_core import thread_store, thread_store_file
from gc_core.thread_store import (
    ThreadSession,
    load_thread_meta,
    update_tui_meta,
)
from gc_core.thread_store_file import FileThreadStore
from gc_testkit.settings import make_settings


async def _session(tmp_path):
    return await ThreadSession.afrom_config(
        settings=make_settings({}),
        system_id="cli",
        thread_id="t",
        store=FileThreadStore(str(tmp_path)),
    )


def _user(mid):
    return {"id": mid, "role": "user", "content": f"вопрос {mid}"}


def _start_tui_write(d, writers):
    writer = threading.Thread(
        target=update_tui_meta,
        args=(d, "cli", "t", {"copy_buffer": "x"}),
        kwargs={"touch": False},
    )
    writers.append(writer)
    writer.start()
    # Замок держит писатель сессии — TUI ждёт; замка нет — успевает.
    writer.join(0.3)


def _tui_write_on_first_read(monkeypatch, d):
    original = thread_store.load_thread_meta
    writers = []

    def load(threads_dir, system_id, thread_id):
        meta = original(threads_dir, system_id, thread_id)
        if not writers:
            _start_tui_write(d, writers)
        return meta

    monkeypatch.setattr(thread_store, "load_thread_meta", load)
    monkeypatch.setattr(thread_store_file, "load_thread_meta", load)
    return writers


def _tui_write_after_archive(monkeypatch, d):
    original = thread_store_file.archive_and_replace_thread_files
    writers = []

    def archive(*args, **kwargs):
        result = original(*args, **kwargs)
        _start_tui_write(d, writers)
        return result

    monkeypatch.setattr(
        thread_store_file, "archive_and_replace_thread_files", archive
    )
    return writers


async def test_step_persist_keeps_tui_key_written_meanwhile(
    tmp_path, monkeypatch
):
    session = await _session(tmp_path)
    writers = _tui_write_on_first_read(monkeypatch, str(tmp_path))

    await session.apersist_batch([_user("u1")], previous_response_id=None)
    writers[0].join()

    meta = load_thread_meta(str(tmp_path), "cli", "t")
    assert meta["tui"]["copy_buffer"] == "x"
    assert meta["messages_count"] == 1


async def test_compaction_keeps_tui_key_written_after_archive(
    tmp_path, monkeypatch
):
    session = await _session(tmp_path)
    await session.apersist_batch([_user("u1")], previous_response_id=None)
    writers = _tui_write_after_archive(monkeypatch, str(tmp_path))

    await session.aarchive_and_compact_summary_payload(
        {"summary_message_content": "сводка", "dialog_steps_offset": 1}
    )
    writers[0].join()

    meta = load_thread_meta(str(tmp_path), "cli", "t")
    assert meta["tui"]["copy_buffer"] == "x"
    assert meta["messages_count"] == 1


def test_update_thread_meta_returns_what_it_wrote(tmp_path):
    from gc_core.thread_store import update_thread_meta

    d = str(tmp_path)
    update_tui_meta(d, "cli", "t", {"status": "idle"})

    written = update_thread_meta(
        d, "cli", "t", lambda meta: {**meta, "token_usage": {"total": 3}}
    )

    assert written["token_usage"] == {"total": 3}
    assert load_thread_meta(d, "cli", "t")["tui"] == {"status": "idle"}


def test_meta_locks_do_not_outlive_their_writers(tmp_path):
    for index in range(5):
        update_tui_meta(str(tmp_path), "cli", f"t{index}", {"status": "x"})
    gc.collect()

    assert len(thread_store._META_LOCKS) == 0
