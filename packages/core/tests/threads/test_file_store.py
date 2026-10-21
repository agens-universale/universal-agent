"""FileThreadStore v2: контракт, формат develop, откат, восстановление."""

import asyncio
import json
import re
from datetime import timedelta

import pytest
from gc_testkit import develop_files as develop
from gc_testkit import develop_files
from gc_core.threads import file_tx
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import (
    LeaseRequest,
    SideChange,
    ThreadChange,
    ThreadKey,
    ThreadQuery,
)
from gc_testkit.legacy_threads import LEGACY_THREADS, install_legacy_threads
from gc_testkit.thread_store_conformance_v2 import CHECKS, assert_conforms

KEY = ThreadKey(system="cli", thread="t1")


def run(coro):
    return asyncio.run(coro)


def _msgs(*ids):
    return [{"id": i, "role": "user", "content": f"текст {i}"} for i in ids]


async def _claim(store, key=KEY):
    record = await store.claim(
        key, LeaseRequest(owner="a", ttl=timedelta(minutes=1))
    )
    return record.lease


@pytest.mark.parametrize("durability", ["atomic", "fsync"])
def test_file_store_conforms(durability, tmp_path):
    counter = iter(range(10_000))

    def factory(clock):
        return FileThreadStore(
            tmp_path / f"s{next(counter)}", durability=durability, clock=clock
        )

    assert assert_conforms(factory) == [c.__name__ for c in CHECKS]


@pytest.mark.parametrize("name", sorted(LEGACY_THREADS))
def test_legacy_thread_reads_like_manifest(name, tmp_path):
    fixture = LEGACY_THREADS[name]
    store = FileThreadStore(install_legacy_threads(tmp_path))
    key = ThreadKey(system=fixture.system, thread=fixture.thread_id)
    record = run(store.get_thread(key))
    assert record is not None
    assert record.info.messages_count == fixture.messages
    assert len(record.archives) == fixture.archives
    assert record.info.title == fixture.title

    async def count(stream):
        return len(await store.read_stream(key, stream))

    assert run(count("messages")) == fixture.messages
    assert run(count("subagent_events")) == fixture.subagent_events
    assert run(count("edits")) == fixture.edits


def test_legacy_threads_are_listed(tmp_path):
    store = FileThreadStore(install_legacy_threads(tmp_path))
    page = run(store.list_threads(ThreadQuery(limit=50)))
    listed = {(r.key.system, r.key.thread) for r in page.items}
    expected = {(t.system, t.thread_id) for t in LEGACY_THREADS.values()}
    assert expected <= listed


def _normalized_lines(path):
    text = path.read_text(encoding="utf-8")
    return re.sub(r'"ts": "[^"]+"', '"ts": "<ts>"', text).splitlines()


def test_history_lines_match_develop_files(tmp_path):
    messages = _msgs("a", "b") + [{"role": "user", "content": "без id"}]
    old_dir = tmp_path / "old"
    develop_files.append_new_thread_messages(
        str(old_dir), "cli", "t1", messages, set()
    )
    store = FileThreadStore(tmp_path / "new")
    lease = run(_claim(store))
    run(
        store.commit(
            KEY,
            ThreadChange(
                op_id="op",
                expected_revision=0,
                lease=lease,
                append_messages=messages,
            ),
        )
    )
    assert _normalized_lines(
        tmp_path / "new" / "data_cli_t1.jsonl"
    ) == _normalized_lines(old_dir / "data_cli_t1.jsonl")


def test_develop_reads_and_writes_v2_thread(tmp_path):
    store = FileThreadStore(tmp_path)
    lease = run(_claim(store))
    run(
        store.commit(
            KEY,
            ThreadChange(
                op_id="op",
                expected_revision=0,
                lease=lease,
                append_messages=_msgs("a"),
            ),
        )
    )
    meta = develop.load_thread_meta(str(tmp_path), "cli", "t1")
    assert meta["messages_count"] == 1
    assert meta["system"] == "cli"
    # Откат: аренда v2 не выставляет owner_id, писатель develop не отказывает.
    develop_files.append_new_thread_messages(
        str(tmp_path), "cli", "t1", _msgs("b"), set(), start_seq=1
    )
    messages, _keys, _seq = develop.load_thread_messages(
        str(tmp_path), "cli", "t1"
    )
    assert [m["id"] for m in messages] == ["a", "b"]


def test_interrupted_write_is_replayed(tmp_path, monkeypatch):
    store = FileThreadStore(tmp_path)
    lease = run(_claim(store))
    real = file_tx._apply_plain

    def crash(directory, change):
        real(directory, change)
        raise RuntimeError("процесс упал посреди записи")

    monkeypatch.setattr(file_tx, "_apply_plain", crash)
    with pytest.raises(RuntimeError):
        run(
            store.commit(
                KEY,
                ThreadChange(
                    op_id="op",
                    expected_revision=0,
                    lease=lease,
                    append_messages=_msgs("a", "b"),
                ),
            )
        )
    monkeypatch.setattr(file_tx, "_apply_plain", real)
    fresh = FileThreadStore(tmp_path)
    entries = run(fresh.read_stream(KEY, "messages"))
    assert [e.data["id"] for e in entries] == ["a", "b"]
    assert run(fresh.get_thread(KEY)).revision == 1


def test_delete_removes_every_thread_file(tmp_path):
    store = FileThreadStore(tmp_path)
    lease = run(_claim(store))
    run(
        store.commit(
            KEY,
            ThreadChange(
                op_id="op1",
                expected_revision=0,
                lease=lease,
                append_messages=_msgs("a"),
                add_aliases=["resp_1"],
            ),
        )
    )
    run(store.put_blob(KEY, b"\x89PNG", mime="image/png"))
    run(
        store.append_side(
            KEY, SideChange(op_id="s", append=[("inbox", [{"text": "x"}])])
        )
    )
    run(store.delete(KEY, op_id="d"))
    left = [
        p.name
        for p in tmp_path.rglob("*")
        if p.is_file() and ".store" not in p.parts
    ]
    assert left == []


def test_legacy_named_thread_moves_to_canonical_file(tmp_path):
    legacy = tmp_path / "data_t1.jsonl"
    line = json.dumps(
        {"type": "message", "seq": 1, "ts": "x", "payload": {"role": "user"}},
        ensure_ascii=False,
    )
    legacy.write_text(line + "\n", encoding="utf-8")
    store = FileThreadStore(tmp_path)
    lease = run(_claim(store))
    run(
        store.commit(
            KEY,
            ThreadChange(
                op_id="op",
                expected_revision=0,
                lease=lease,
                append_messages=_msgs("b"),
            ),
        )
    )
    canonical = (tmp_path / "data_cli_t1.jsonl").read_text(encoding="utf-8")
    assert canonical.splitlines()[0] == line, (
        "старая строка перенесена как есть"
    )
    entries = run(store.read_stream(KEY, "messages"))
    assert [e.seq for e in entries] == [1, 2]
