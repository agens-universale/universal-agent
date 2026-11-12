"""``SQLiteThreadStore`` проходит конформанс контракта v2 и набор пути хода
(сессия агента поверх него); картинки треда — вложения в базе."""

import os
from datetime import timedelta

import pytest
from gc_core.threads.models import LeaseRequest, ThreadKey
from gc_stores.sqlite import SQLiteThreadStore
from gc_stores.sqlite.database import connect
from gc_testkit.session_checks import CHECKS as SESSION_CHECKS
from gc_testkit.session_checks import aassert_session_works
from gc_testkit.thread_store_conformance_v2 import CHECKS, assert_conforms

KEY = ThreadKey(system="cli", thread="lite")


def test_sqlite_store_conforms(tmp_path):
    made = iter(range(1000))

    def make(clock):
        return SQLiteThreadStore(tmp_path / f"s{next(made)}.db", clock=clock)

    assert assert_conforms(make) == [check.__name__ for check in CHECKS]


async def test_session_over_sqlite_works(tmp_path):
    made = iter(range(1000))

    def make():
        return SQLiteThreadStore(tmp_path / f"p{next(made)}.db")

    passed = await aassert_session_works(make)
    assert passed == [check.__name__ for check in SESSION_CHECKS]


async def test_thread_images_live_in_the_database(tmp_path):
    """Картинки ядра — вложения треда (``put_blob``, ссылка
    ``thread-file://``) в базе, удаляются с ним; синхронной возможности
    файлов треда у хранилища больше нет."""
    from gc_core.thread_files import blob_ref_of

    store = SQLiteThreadStore(tmp_path / "t.db")
    await store.claim(KEY, LeaseRequest(owner="a", ttl=timedelta(minutes=1)))
    ref = await store.put_blob(KEY, b"\x89PNG", mime="image/png")
    assert await store.get_blob(blob_ref_of(ref.location)) == b"\x89PNG"
    await store.delete(KEY, op_id="d")
    with pytest.raises(FileNotFoundError):
        await store.get_blob(ref)
    assert not hasattr(store, "write_thread_file")


def test_protected_paths_cover_the_journals(tmp_path):
    path = str(tmp_path / "t.db")
    assert SQLiteThreadStore(path).protected_paths() == [
        path,
        f"{path}-wal",
        f"{path}-shm",
        f"{path}-journal",
    ]


@pytest.mark.skipif(os.name == "nt", reason="права POSIX")
async def test_database_is_private_to_its_owner(tmp_path):
    """В базе история и квитанции агента: файл и журналы — только владельцу."""
    path = tmp_path / "t.db"
    store = SQLiteThreadStore(path)
    await store.claim(KEY, LeaseRequest(owner="a", ttl=timedelta(minutes=1)))
    # Журналы WAL живут, пока открыто соединение.
    conn = connect(store.path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM gc_receipts WHERE 0")
        conn.execute("COMMIT")
        journals = [tmp_path / "t.db-wal", tmp_path / "t.db-shm"]
        assert all(j.exists() for j in journals)
        for name in (path, *journals):
            assert name.stat().st_mode & 0o777 == 0o600, name
    finally:
        conn.close()


async def test_read_stream_limit_zero_reads_nothing(tmp_path):
    from gc_core.threads.models import SideChange

    store = SQLiteThreadStore(tmp_path / "t.db")
    await store.append_side(
        KEY, SideChange(op_id="s", append=[("subagent_events", [{"k": 1}])])
    )
    assert await store.read_stream(KEY, "subagent_events", limit=0) == []
    assert len(await store.read_stream(KEY, "subagent_events")) == 1


async def test_prune_drops_old_tombstones(tmp_path):
    from gc_testkit.thread_store_conformance_v2 import (
        FakeClock,
        acheck_prune_drops_old_tombstones,
    )

    clock = FakeClock()
    await acheck_prune_drops_old_tombstones(
        SQLiteThreadStore(tmp_path / "t.db", clock=clock), clock
    )
