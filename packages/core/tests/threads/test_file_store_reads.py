"""Чтение FileThreadStore v2: без побочных записей на диск и за O(дельты)
при дочитывании хвоста (опрос TUI — 250 мс, chat и ACP — 50 мс)."""

import asyncio
import json
from datetime import timedelta

from gc_core.threads import file_store as file_store_module
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import LeaseRequest, ThreadChange, ThreadKey
from gc_testkit import develop_files
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
    develop_files.append_new_thread_messages(
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
    develop_files.discard_message_by_id(str(tmp_path), "cli", "t1", "m1")
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


def test_last_line_without_newline_counts_when_it_is_whole(tmp_path):
    lines = [
        json.dumps(
            {
                "type": "message",
                "seq": n,
                "ts": "x",
                "payload": {"id": f"m{n}"},
            }
        )
        for n in (1, 2)
    ]
    data = tmp_path / "data_cli_t1.jsonl"
    data.write_text("\n".join(lines), encoding="utf-8")  # без \n в конце
    store = FileThreadStore(tmp_path)
    entries = run(store.read_stream(KEY, "messages"))
    assert [e.data["id"] for e in entries] == ["m1", "m2"]
    with data.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n"
            + lines[0].replace('"m1"', '"m3"').replace('"seq": 1', '"seq": 3')
            + "\n"
        )
    tail = run(store.read_stream(KEY, "messages", after_seq=2))
    assert [e.data["id"] for e in tail] == ["m3"]


def test_list_since_skips_stale_threads_before_reading_meta(
    tmp_path, monkeypatch
):
    """Находка ревью №14: восстановление обходит треды, обновлённые за
    окно; тред, чьи файлы старше окна, отсеивается по mtime — его meta не
    читается."""
    import os
    import time
    from datetime import UTC, datetime, timedelta

    from gc_core.threads import file_layout
    from gc_core.threads.facade import Threads
    from gc_core.threads.models import ThreadQuery

    store = FileThreadStore(tmp_path)
    old = ThreadKey(system="cli", thread="old")
    fresh = ThreadKey(system="cli", thread="fresh")
    for key in (old, fresh):
        run(Threads(store).append_messages(key, _msgs(0, 1)))
    two_days_ago = time.time() - 2 * 86400
    for path in tmp_path.glob("*_old*"):
        os.utime(path, (two_days_ago, two_days_ago))
    store_dir = file_layout_store_dir(tmp_path, old)
    for path in [store_dir, *store_dir.iterdir()]:
        os.utime(path, (two_days_ago, two_days_ago))
    read: list = []
    real = file_layout.read_meta_file
    monkeypatch.setattr(
        file_layout,
        "read_meta_file",
        lambda path: read.append(os.path.basename(path)) or real(path),
    )
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    page = run(
        FileThreadStore(tmp_path).list_threads(
            ThreadQuery(updated_after=since)
        )
    )
    assert [r.key for r in page.items] == [fresh]
    assert "meta_cli_old.json" not in read


def file_layout_store_dir(root, key):
    import hashlib

    stem = f"{key.system}_{key.thread}"
    return root / ".store" / hashlib.sha256(stem.encode()).hexdigest()


def _spy_listed(monkeypatch) -> list:
    """Ключи тредов, прочитанных списком целиком (блокировка, сборка
    записи)."""
    listed: list = []
    real = FileThreadStore._listed

    def spy(self, key):
        listed.append(key)
        return real(self, key)

    monkeypatch.setattr(FileThreadStore, "_listed", spy)
    return listed


def _fill(store, counts: dict) -> None:
    from gc_core.threads.facade import Threads

    threads = Threads(store)
    for system, count in counts.items():
        for n in range(count):
            key = ThreadKey(system=system, thread=f"{system}{n}")
            run(threads.update_frontend(key, "x", {"n": n}))


def test_list_of_one_system_reads_only_its_threads(tmp_path, monkeypatch):
    """Список одной системы (диалоги chat, сессии ACP, задачи A2A) не
    читает треды других: при тысячах тредов вебхука страница диалогов
    стоила секунды на цикле событий."""
    from gc_core.threads.models import ThreadQuery

    store = FileThreadStore(tmp_path)
    _fill(store, {"a2a": 3, "load": 20})
    listed = _spy_listed(monkeypatch)
    page = run(store.list_threads(ThreadQuery(system="a2a")))
    assert sorted(r.key.thread for r in page.items) == ["a2a0", "a2a1", "a2a2"]
    assert {key.system for key in listed} == {"a2a"}


def test_iterating_a_file_store_reads_each_thread_once(tmp_path, monkeypatch):
    """Каждая страница списка файлового хранилища читает весь каталог:
    постраничный обход — N/страница полных проходов. Фасад обходит его
    одним проходом, порядок тот же."""
    from gc_core.threads import facade as facade_module
    from gc_core.threads.facade import Threads

    monkeypatch.setattr(facade_module, "_PAGE", 5)
    store = FileThreadStore(tmp_path)
    _fill(store, {"a2a": 4, "load": 8})
    threads = Threads(store)

    async def collect(**query):
        return [record async for record in threads.iter_threads(**query)]

    listed = _spy_listed(monkeypatch)
    found = run(collect())
    assert len(found) == 12
    assert len(listed) == 12
    stamps = [record.info.updated_at for record in found]
    assert stamps == sorted(stamps, reverse=True)
    listed.clear()
    only = run(collect(system="a2a"))
    assert sorted(r.key.thread for r in only) == [f"a2a{n}" for n in range(4)]
    assert len(listed) == 4
