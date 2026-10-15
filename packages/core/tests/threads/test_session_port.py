"""Порт сессии поверх контракта v2 неотличим от старого хранилища:
он проходит старый асинхронный конформанс-набор целиком."""

import pytest
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.session_port import SessionPort
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from gc_testkit.thread_store_conformance import assert_conforms_async


@pytest.mark.parametrize("backend", ["memory", "file"])
def test_session_port_keeps_old_contract(backend, tmp_path):
    counter = iter(range(10_000))

    def factory():
        if backend == "memory":
            return SessionPort(MemoryThreadStore())
        return SessionPort(FileThreadStore(tmp_path / f"s{next(counter)}"))

    assert len(assert_conforms_async(factory)) == 18


class _Recording(MemoryThreadStore):
    """Хранилище, запоминающее ``op_id`` записей и число ``claim``."""

    def __init__(self) -> None:
        super().__init__()
        self.op_ids: list[str] = []
        self.claims = 0

    async def commit(self, key, change):
        self.op_ids.append(change.op_id)
        return await super().commit(key, change)

    async def claim(self, key, lease):
        self.claims += 1
        return await super().claim(key, lease)


async def _two_steps(port: SessionPort) -> None:
    for n in range(2):
        await port.aappend_messages(
            "wf", "t", [{"id": f"m{n}", "role": "user", "content": str(n)}]
        )


async def test_op_ids_come_from_the_execution_environment():
    """В workflow ``op_id`` — из детерминированного генератора: replay и
    повтор activity дают те же операции."""
    from datetime import UTC, datetime
    from itertools import count

    from gc_core.execution.environment import execution_environment

    runs = []
    for _ in range(2):
        seq = count()
        store = _Recording()
        with execution_environment(
            new_id=lambda prefix, seq=seq: f"{prefix}-{next(seq)}",
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        ):
            await _two_steps(SessionPort(store, owner="wf", ttl=None))
        runs.append(store.op_ids)
    assert runs[0] == runs[1] and len(runs[0]) == 2
    assert all(op.startswith("session-") for op in runs[0])


async def test_untimed_lease_is_claimed_once():
    """Бессрочная аренда (владелец — workflow) не перебирается на каждой
    записи: лишний ``claim`` под Temporal — лишняя activity."""
    store = _Recording()
    await _two_steps(SessionPort(store, owner="wf", ttl=None))
    assert store.claims == 1


async def test_adopted_lease_needs_no_claim():
    from gc_core.threads.models import LeaseRequest, ThreadKey

    store = _Recording()
    key = ThreadKey(system="wf", thread="t")
    record = await store.claim(key, LeaseRequest(owner="wf", ttl=None))
    port = SessionPort(store, owner="wf", ttl=None)
    port.adopt_lease(key, record.lease)
    await _two_steps(port)
    assert store.claims == 1, "запись взяла аренду заново"
