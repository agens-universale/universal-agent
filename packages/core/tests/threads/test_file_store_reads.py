"""Чтение FileThreadStore v2: без побочных записей на диск и за O(дельты)
при дочитывании хвоста (опрос TUI — 250 мс, chat и ACP — 50 мс)."""

import asyncio
import json
from datetime import timedelta

from gc_core import thread_store as develop
from gc_core.threads import file_store as file_store_module
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import LeaseRequest, ThreadChange, ThreadKey
from gc_testkit.legacy_threads import LEGACY_THREADS, install_legacy_threads

KEY = ThreadKey(system="cli", thread="t1")


def run(coro):
    return asyncio.run(coro)


def _msgs(start, count):
    return [
        {"id": f"m{n}", "role": "user", "content": f"текст {n}"}
        for n in range(start, start + count)
    ]


async def _append(store, messages, op):
    record = await store.claim(
        KEY, LeaseRequest(owner="host", ttl=timedelta(minutes=1))
    )
    await store.commit(
        KEY,
        ThreadChange(
            op_id=op,
            expected_revision=record.revision,
            lease=record.lease,
            append_messages=messages,
        ),
    )


def _files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_reading_unknown_thread_writes_nothing(tmp_path):
    store = FileThreadStore(tmp_path)
    assert run(store.get_thread(KEY)) is None
    assert run(store.read_stream(KEY, "messages")) == []
    assert _files(tmp_path) == []


def test_reading_develop_thread_creates_no_store_dir(tmp_path):
    fixture = LEGACY_THREADS["cli_two_turns"]
    threads = install_legacy_threads(tmp_path)
    before = _files(tmp_path)
    store = FileThreadStore(threads)
    key = ThreadKey(system=fixture.system, thread=fixture.thread_id)
    assert run(store.get_thread(key)) is not None
    assert len(run(store.read_stream(key, "messages"))) == fixture.messages
    assert _files(tmp_path) == before


def test_tail_read_starts_from_remembered_offset(tmp_path, monkeypatch):
    store = FileThreadStore(tmp_path)
    run(_append(store, _msgs(0, 1000), "op1"))
    head = run(store.read_stream(KEY, "messages"))
    offsets = []
    real = file_store_module._read_lines
    monkeypatch.setattr(
        file_store_module,
        "_read_lines",
        lambda path, offset: offsets.append(offset) or real(path, offset),
    )
    run(_append(store, _msgs(1000, 1), "op2"))
    offsets.clear()
    tail = run(store.read_stream(KEY, "messages", after_seq=head[-1].seq))
    assert [e.data["id"] for e in tail] == ["m1000"]
    assert offsets and min(offsets) > 0, "хвост читался с начала файла"


def test_tail_sees_external_append_and_waits_for_whole_line(tmp_path):
    store = FileThreadStore(tmp_path)
    run(_append(store, _msgs(0, 3), "op1"))
    last = run(store.read_stream(KEY, "messages"))[-1].seq
    develop.append_new_thread_messages(
        str(tmp_path), "cli", "t1", _msgs(3, 1), set(), start_seq=last
    )
    data = tmp_path / "data_cli_t1.jsonl"
    line = json.dumps(
        {"type": "message", "seq": 5, "ts": "x", "payload": {"id": "m4"}}
    )
    with data.open("a", encoding="utf-8") as stream:
        stream.write(line[:20])  # строку дописывают прямо сейчас
    tail = run(store.read_stream(KEY, "messages", after_seq=last))
    assert [e.data["id"] for e in tail] == ["m3"]
    with data.open("a", encoding="utf-8") as stream:
        stream.write(line[20:] + "\n")
    tail = run(store.read_stream(KEY, "messages", after_seq=tail[-1].seq))
    assert [e.data["id"] for e in tail] == ["m4"]


def test_rewritten_file_is_read_whole_again(tmp_path):
    store = FileThreadStore(tmp_path)
    run(_append(store, _msgs(0, 3), "op1"))
    run(store.read_stream(KEY, "messages"))
    develop.discard_message_by_id(str(tmp_path), "cli", "t1", "m1")
    entries = run(store.read_stream(KEY, "messages"))
    assert [e.data["id"] for e in entries] == ["m0", "m2"]


def test_cache_from_older_stage_without_offsets_still_reads(tmp_path):
    store = FileThreadStore(tmp_path)
    run(_append(store, _msgs(0, 3), "op1"))
    control = next(tmp_path.glob(".store/*/control.json"))
    doc = json.loads(control.read_text())
    for stats in doc["v2"]["stats"].values():
        stats.pop("recent", None)
    control.write_text(json.dumps(doc))
    fresh = FileThreadStore(tmp_path)
    tail = run(fresh.read_stream(KEY, "messages", after_seq=1))
    assert [e.data["id"] for e in tail] == ["m1", "m2"]
