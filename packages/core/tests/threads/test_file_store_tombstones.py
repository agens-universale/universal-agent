"""Что остаётся от удалённого треда в ``.store/`` и кто это убирает.

Надгробие (эпоха и поколение удалённого треда) нужно контракту, но не
списку тредов и не диску навсегда: gc-chat заводит и удаляет тред на каждый
completions. Тред, удалённый кодом develop (он не знает потоков фронтендов и
алиасов), не оживает призраком. Квитанции тред чистит сам, раз в сутки.
"""

import asyncio
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from gc_core.thread_store_journal import _locked_file
from gc_core.threads import file_layout as layout
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import (
    LeaseRequest,
    SideChange,
    ThreadChange,
    ThreadKey,
    ThreadQuery,
)

KEY = ThreadKey(system="completions", thread="req-1")
_TTL = timedelta(seconds=60)


def _run(coro):
    return asyncio.run(coro)


def _write(store, key=KEY, op="op-1", text="hi", stream=None, alias=None):
    lease = _run(store.claim(key, LeaseRequest(owner="host", ttl=_TTL))).lease
    record = _run(store.get_thread(key))
    _run(
        store.commit(
            key,
            ThreadChange(
                op_id=op,
                expected_revision=record.revision,
                lease=lease,
                append_messages=[{"id": op, "role": "user", "content": text}],
                add_aliases=[alias] if alias else [],
            ),
        )
    )
    if stream:
        _run(
            store.append_side(
                key, SideChange(op_id=f"{op}-side", append=[(stream, [{}])])
            )
        )
    _run(store.release(key, lease))


def _store_dirs(root) -> list[str]:
    store = root / ".store"
    return (
        sorted(p.name for p in store.iterdir() if p.is_dir())
        if store.is_dir()
        else []
    )


def _age(root, seconds=2 * 86400) -> None:
    past = time.time() - seconds
    for path in (root / ".store").rglob("*"):
        os.utime(path, (past, past))


def test_tombstone_is_not_listed_and_goes_away_with_the_sweep(tmp_path):
    store = FileThreadStore(tmp_path)
    for n in range(5):
        key = ThreadKey(system="completions", thread=f"req-{n}")
        _write(store, key)
        _run(store.delete(key, op_id=f"delete-{n}"))
    assert not list(tmp_path.glob(".store/*/identity.json"))
    assert _run(store.list_threads(ThreadQuery())).items == []
    # Свежие надгробия держат эпоху (контракт), старые уходят.
    assert store.sweep_tombstones() == 0
    _age(tmp_path)
    assert store.sweep_tombstones() > 0
    assert _store_dirs(tmp_path) == []
    # Ключ свободен: тред заводится заново.
    _write(store, ThreadKey(system="completions", thread="req-0"))
    assert len(_run(store.list_threads(ThreadQuery())).items) == 1


def test_sweep_keeps_live_threads(tmp_path):
    store = FileThreadStore(tmp_path)
    _write(store)
    _age(tmp_path)
    store.sweep_tombstones()
    record = _run(store.get_thread(KEY))
    assert record is not None and record.revision == 1
    _write(store, op="op-2")
    assert _run(store.get_thread(KEY)).revision == 2


def test_delete_sweeps_old_tombstones_once_a_day(tmp_path):
    store = FileThreadStore(tmp_path)
    old = ThreadKey(system="completions", thread="old")
    _write(store, old)
    _run(store.delete(old, op_id="d-old"))
    _age(tmp_path)  # и надгробие, и отметка уборки — двухдневной давности
    _write(store)
    _run(store.delete(KEY, op_id="d-new"))
    names = [
        n
        for n in _store_dirs(tmp_path)
        if (tmp_path / ".store" / n / "control.json").exists()
    ]
    assert len(names) == 1, "старое надгробие осталось"
    mark = tmp_path / ".store" / ".tombstones-swept"
    assert time.time() - mark.stat().st_mtime < 60


@pytest.mark.skipif(
    os.name == "nt",
    reason="flock; на Windows каталог с открытой блокировкой не переименовать",
)
def test_waiter_on_a_swept_lock_takes_a_fresh_one(tmp_path):
    """Процесс ждал блокировку каталога, который уборка тем временем убрала:
    он получает блокировку заново, по живому пути, а не по удалённому
    файлу (иначе двое писали бы в тред одновременно)."""
    base = tmp_path / ".store" / "x"
    got: list[int] = []
    started = threading.Event()

    def waiter():
        started.set()
        with _locked_file(base) as stream:
            got.append(os.fstat(stream.fileno()).st_ino)
            got.append(os.stat(base / "lock").st_ino)

    with _locked_file(base):
        worker = threading.Thread(target=waiter)
        worker.start()
        started.wait()
        time.sleep(0.2)  # ждущий открыл файл и стоит на flock
        os.rename(base, tmp_path / ".store" / ".trash-x")
    worker.join(5)
    assert len(got) == 2 and got[0] == got[1]


def test_thread_deleted_by_develop_does_not_come_back(tmp_path):
    """develop удаляет meta и историю, а поток фронтенда и алиас оставляет:
    тред не виден, алиас ведёт в никуда, новый тред под тем же ключом не
    дочитывает чужие записи."""
    store = FileThreadStore(tmp_path)
    _write(store, stream="chat.responses", alias="resp_1")
    d, s, t = str(tmp_path), KEY.system, KEY.thread
    os.unlink(layout.meta_file(d, s, t))
    os.unlink(layout.history_file(d, s, t))
    fresh = FileThreadStore(tmp_path)
    assert _run(fresh.get_thread(KEY)) is None
    assert _run(fresh.list_threads(ThreadQuery())).items == []
    assert _run(fresh.resolve_alias("resp_1")) is None
    _write(fresh, op="op-2")
    assert _run(fresh.read_stream(KEY, "chat.responses")) == []
    assert not os.path.exists(layout.stream_path(d, s, t, "chat.responses"))


def test_delete_of_thread_deleted_by_develop_removes_leftovers(tmp_path):
    store = FileThreadStore(tmp_path)
    _write(store, stream="chat.responses", alias="resp_1")
    d, s, t = str(tmp_path), KEY.system, KEY.thread
    os.unlink(layout.meta_file(d, s, t))
    os.unlink(layout.history_file(d, s, t))
    _run(FileThreadStore(tmp_path).delete(KEY, op_id="janitor"))
    assert not os.path.exists(layout.stream_path(d, s, t, "chat.responses"))
    assert not list((tmp_path / "aliases").glob("*.json"))


def test_delete_leaves_streams_of_a_lookalike_thread(tmp_path):
    """``_`` допустим и в system, и в thread_id: имя потока треда
    ``(responses, foo_cli_a_b)`` подходит под маску ``stream_*_cli_a_b``."""
    store = FileThreadStore(tmp_path)
    mine = ThreadKey(system="cli", thread="a_b")
    other = ThreadKey(system="responses", thread="foo_cli_a_b")
    _write(store, mine, stream="chat.responses")
    _write(store, other, op="op-o", stream="chat.responses")
    _run(store.delete(mine, op_id="d"))
    assert len(_run(store.read_stream(other, "chat.responses"))) == 1


def test_corrupt_alias_file_is_no_alias(tmp_path):
    store = FileThreadStore(tmp_path)
    _write(store, alias="resp_1")
    (alias,) = (tmp_path / "aliases").glob("*.json")
    alias.write_text("{not json", encoding="utf-8")
    assert _run(store.resolve_alias("resp_1")) is None


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_thread_prunes_its_own_old_receipts_on_write(tmp_path):
    clock = _Clock()
    store = FileThreadStore(tmp_path, clock=clock)
    _write(store, op="op-1")
    receipts = next(tmp_path.glob(".store/*/receipts"))
    first = len(list(receipts.glob("*.json")))
    clock.now += timedelta(days=8)
    _write(store, op="op-2")
    assert len(list(receipts.glob("*.json"))) == first, (
        "квитанции недельной давности не убраны"
    )
