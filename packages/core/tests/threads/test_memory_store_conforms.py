"""Эталонная MemoryThreadStore проходит конформанс-набор контракта v2,
а набор ловит хранилище, нарушающее контракт."""

import pytest
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from gc_testkit.thread_store_conformance_v2 import (
    CHECKS,
    ConformanceError,
    assert_conforms,
)


def test_memory_store_conforms():
    passed = assert_conforms(lambda clock: MemoryThreadStore(clock=clock))
    assert passed == [check.__name__ for check in CHECKS]


class _NoRevisionCheck(MemoryThreadStore):
    """Сломанная реализация: пишет при любой expected_revision."""

    async def commit(self, key, change):
        thread = self._threads.get(key)
        if thread is not None:
            change = change.model_copy(
                update={"expected_revision": thread.revision}
            )
        return await super().commit(key, change)


class _NoFencing(MemoryThreadStore):
    """Сломанная реализация: не сверяет эпоху аренды."""

    def _check_lease(self, thread, lease):
        if thread is None or thread.lease is None:
            super()._check_lease(thread, lease)


class _ReusesSeq(MemoryThreadStore):
    """Сломанная реализация: удалённый хвост освобождает номер seq."""

    def _discard(self, thread, ids):
        super()._discard(thread, ids)
        history = thread.streams.get("messages", [])
        thread.high["messages"] = max((e.seq for e in history), default=0)


@pytest.mark.parametrize(
    ("broken", "check"),
    [
        (_NoRevisionCheck, "check_revision_conflict"),
        (_NoFencing, "check_lease_expiry_and_fencing"),
        (_ReusesSeq, "check_seq_never_reused"),
    ],
)
def test_conformance_catches_broken_store(broken, check):
    with pytest.raises(ConformanceError, match=check):
        assert_conforms(lambda clock: broken(clock=clock))
