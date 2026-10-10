"""Файловое durable-хранилище проходит общий durable-конформанс."""

import itertools

from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_testkit.durable_thread_store_conformance import (
    BEHAVIOUR_CHECKS,
    STRUCTURE_CHECKS,
    assert_durable_conforms,
)


def test_durable_file_store_conforms(tmp_path):
    counter = itertools.count()
    passed = assert_durable_conforms(
        lambda: DurableFileThreadStore(str(tmp_path / str(next(counter))))
    )
    assert len(passed) == len(STRUCTURE_CHECKS) + len(BEHAVIOUR_CHECKS)
