"""SQLite durable-хранилище проходит тот же конформанс, что и файловое."""

import itertools

from gc_testkit.durable_thread_store_conformance import (
    BEHAVIOUR_CHECKS,
    STRUCTURE_CHECKS,
    assert_durable_conforms,
)
from gc_core_temporalio.storage.sqlite import SQLiteThreadStore


def test_sqlite_store_conforms(tmp_path):
    counter = itertools.count()
    passed = assert_durable_conforms(
        lambda: SQLiteThreadStore(str(tmp_path / f"{next(counter)}.sqlite"))
    )
    assert len(passed) == len(STRUCTURE_CHECKS) + len(BEHAVIOUR_CHECKS)
