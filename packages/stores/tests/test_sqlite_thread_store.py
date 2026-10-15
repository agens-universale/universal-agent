"""``SQLiteThreadStore`` проходит конформанс контракта v2, порт сессии
поверх него — старый асинхронный набор; файлы треда живут в базе."""

from gc_core.threads.models import LeaseRequest, ThreadKey
from gc_core.threads.session_port import SessionPort
from gc_stores.sqlite import SQLiteThreadStore
from gc_testkit.thread_store_conformance import assert_conforms_async
from gc_testkit.thread_store_conformance_v2 import CHECKS, assert_conforms

KEY = ThreadKey(system="cli", thread="lite")


def test_sqlite_store_conforms(tmp_path):
    made = iter(range(1000))

    def make(clock):
        return SQLiteThreadStore(tmp_path / f"s{next(made)}.db", clock=clock)

    assert assert_conforms(make) == [check.__name__ for check in CHECKS]


def test_session_port_over_sqlite_keeps_old_contract(tmp_path):
    made = iter(range(1000))

    def factory():
        return SessionPort(SQLiteThreadStore(tmp_path / f"p{next(made)}.db"))

    assert len(assert_conforms_async(factory)) == 18


async def test_thread_files_live_in_the_database(tmp_path):
    """Картинки ядра (``thread-file://``) — в базе треда, удаляются с ним."""
    store = SQLiteThreadStore(tmp_path / "t.db")
    await store.claim(KEY, LeaseRequest(owner="a"))
    store.write_thread_file("cli", "lite", "images/a.png", b"\x89PNG")
    assert store.read_thread_file("cli", "lite", "images/a.png") == b"\x89PNG"
    assert store.read_thread_file("cli", "lite", "images/none.png") is None
    await store.delete(KEY, op_id="d")
    assert store.read_thread_file("cli", "lite", "images/a.png") is None


def test_protected_paths_cover_the_journals(tmp_path):
    path = str(tmp_path / "t.db")
    assert SQLiteThreadStore(path).protected_paths() == [
        path,
        f"{path}-wal",
        f"{path}-shm",
        f"{path}-journal",
    ]
