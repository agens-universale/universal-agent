"""Контракт ThreadStore v2: состав методов и ``watch`` по умолчанию."""

import asyncio
import inspect

import pytest
from gc_core.threads.models import StreamEntry, ThreadKey
from gc_core.threads.store import CONTRACT_METHODS, ThreadStore

KEY = ThreadKey(system="cli", thread="t")


def test_contract_has_thirteen_async_methods():
    assert len(CONTRACT_METHODS) == 13
    assert set(CONTRACT_METHODS) == ThreadStore.__abstractmethods__
    for name in CONTRACT_METHODS:
        assert inspect.iscoroutinefunction(getattr(ThreadStore, name)), name


def test_incomplete_store_cannot_be_created():
    class Half(ThreadStore):
        async def get_thread(self, key):
            return None

    with pytest.raises(TypeError):
        Half()


class _Polled(ThreadStore):
    """Заглушка: поток растёт по одной записи на каждый опрос."""

    def __init__(self):
        self.calls = 0

    async def read_stream(self, key, stream, *, after_seq=0, limit=None):
        self.calls += 1
        entry = StreamEntry(seq=self.calls, ts="t", data={"n": self.calls})
        return [entry] if entry.seq > after_seq else []


for _name in CONTRACT_METHODS:
    if _name != "read_stream":
        setattr(_Polled, _name, getattr(ThreadStore, _name))
_Polled.__abstractmethods__ = frozenset()


async def test_watch_polls_new_entries_until_cancelled():
    store = _Polled()
    seen = []

    async def consume():
        async for entry in store.watch(
            KEY, "messages", after_seq=0, poll_interval=0
        ):
            seen.append(entry.seq)

    task = asyncio.create_task(consume())
    while len(seen) < 3:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen[:3] == [1, 2, 3]
