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
