"""Большой вывод тула (сообщение в мегабайты) пишется и читается целиком
каждым хранилищем на SQL-движке."""

from datetime import timedelta

import pytest
from gc_core.threads.models import LeaseRequest, ThreadChange, ThreadKey
from gc_stores.postgres import PostgresThreadStore
from gc_stores.sqlite import SQLiteThreadStore

KEY = ThreadKey(system="s", thread="big")
#: ~5 МБ в UTF-8: кириллица — два байта на символ.
BIG = {"role": "tool", "content": "я" * (5 * 1024 * 1024 // 2)}


@pytest.fixture(params=["sqlite", "postgres"])
async def store(request, tmp_path):
    if request.param == "sqlite":
        made = SQLiteThreadStore(tmp_path / "big.db")
    else:
        dsn = request.getfixturevalue("postgres_dsn")
        schema = await request.getfixturevalue("schemas").acreate()
        made = PostgresThreadStore(dsn, schema=schema)
    yield made
    await made.close()


async def test_five_megabyte_message_round_trips(store):
    record = await store.claim(
        KEY, LeaseRequest(owner="w", ttl=timedelta(minutes=1))
    )
    await store.commit(
        KEY,
        ThreadChange(
            op_id="o",
            expected_revision=0,
            lease=record.lease,
            append_messages=[BIG],
        ),
    )
    (entry,) = await store.read_stream(KEY, "messages")
    assert entry.data == BIG
