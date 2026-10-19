"""Заголовок треда KV переживает сериализацию без потерь."""

import math
from datetime import UTC, datetime, timedelta

from gc_core.threads.models import (
    Lease,
    RuntimeStatus,
    SessionState,
    ThreadInfo,
    ThreadKey,
)
from gc_stores.kv.head import Chunk, Head

KEY = ThreadKey(system="cli", thread="t")
NOW = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=UTC)


def test_round_trip_keeps_fields_set_and_non_finite_numbers():
    head = Head.fresh(KEY, NOW)
    head.row.info = ThreadInfo(title="заголовок")
    head.row.state = SessionState(notes="заметки")
    head.row.runtime = RuntimeStatus(turn="running")
    head.row.extra = {"x": math.inf}
    head.row.grant(
        Lease(owner="w", epoch=2, expires_at=NOW + timedelta(minutes=1))
    )
    head.streams["messages"] = [Chunk("c1", 1, 3, 2)]
    head.archive_chunks["a1"] = [Chunk("c0", 1, 5, 5)]
    head.aliases = ["al"]
    head.pending = [
        {"op_id": "o", "digest": "d", "receipt": "{}", "at": NOW.isoformat()}
    ]
    head.index = "idx/1/2/3"
    again = Head.loads(head.dumps())
    assert again.row.info.model_fields_set == {"title"}
    assert again.row.state.model_fields_set == {"notes"}
    assert again.row.runtime.model_fields_set == {"turn"}
    assert again.row.extra["x"] == math.inf
    assert again.row.lease == head.row.lease
    assert again.row.created_at == NOW
    assert again.streams == head.streams
    assert again.archive_chunks == head.archive_chunks
    assert (again.aliases, again.pending, again.index) == (
        head.aliases,
        head.pending,
        head.index,
    )


def test_fresh_after_tombstone_continues_generation_and_epoch():
    head = Head.fresh(KEY, NOW)
    head.row.generation, head.row.lease_epoch = 4, 7
    tomb = Head.loads(head.tombstone().dumps())
    assert tomb.deleted and tomb.row.generation == 5
    again = Head.fresh(KEY, NOW, tomb)
    assert (again.row.generation, again.row.lease_epoch) == (5, 7)
    assert not again.deleted
