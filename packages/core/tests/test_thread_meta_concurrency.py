"""Мета треда при параллельных писателях одного процесса.

TUI (``meta.tui``) и исполнитель (``meta.runtime``) пишут мету одного
треда из разных потоков; читатель не должен видеть обрезанный файл, писатель
— падать на чужом временном файле, а ключи другого писателя — теряться.
"""

import threading

from gc_core.thread_store import (
    discard_message_by_id,
    load_thread_meta,
    update_runtime_meta,
    update_tui_meta,
)


def _race(d, tid, writers, seconds=1.0):
    errors = []
    stop = threading.Event()

    def writer(fn, key):
        i = 0
        while not stop.is_set():
            try:
                fn(d, "cli", tid, {key: "x" * 2000 + str(i)})
            except Exception as exc:
                errors.append(("write", type(exc).__name__))
            i += 1

    def reader():
        while not stop.is_set():
            try:
                load_thread_meta(d, "cli", tid)
            except Exception as exc:
                errors.append(("read", type(exc).__name__))

    threads = [
        threading.Thread(target=writer, args=pair) for pair in writers
    ] + [threading.Thread(target=reader)]
    for t in threads:
        t.start()
    stop.wait(seconds)
    stop.set()
    for t in threads:
        t.join()
    return sorted(set(errors))


def test_concurrent_meta_writers_never_expose_a_broken_file(tmp_path):
    d, tid = str(tmp_path), "t1"
    update_tui_meta(d, "cli", tid, {"status": "running"})

    errors = _race(
        d,
        tid,
        [
            (update_tui_meta, "status"),
            (update_runtime_meta, "live_history_chars"),
        ],
    )

    assert errors == []


def test_concurrent_meta_writers_keep_each_others_keys(tmp_path):
    d, tid = str(tmp_path), "t2"
    update_tui_meta(d, "cli", tid, {"status": "running"})

    _race(
        d,
        tid,
        [
            (update_tui_meta, "status"),
            (update_runtime_meta, "live_history_chars"),
        ],
    )
    meta = load_thread_meta(d, "cli", tid)

    assert meta["tui"]["status"].startswith("x")
    assert meta["runtime"]["live_history_chars"].startswith("x")


def test_discard_leaves_no_fixed_tmp_next_to_data(tmp_path):
    d, tid = str(tmp_path), "t3"
    data = tmp_path / f"data_cli_{tid}.jsonl"
    data.write_text(
        '{"type": "message", "seq": 1,'
        ' "payload": {"id": "m1", "role": "user", "content": "a"}}\n',
        encoding="utf-8",
    )
    (tmp_path / f"data_cli_{tid}.jsonl.tmp").mkdir()

    assert discard_message_by_id(d, "cli", tid, "m1") is True
    assert data.read_text(encoding="utf-8") == ""
