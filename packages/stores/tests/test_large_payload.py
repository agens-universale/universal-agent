"""Большой вывод тула (сообщение в мегабайты) пишется и читается целиком
каждым хранилищем тредов на базах."""

from datetime import timedelta

import pytest
from gc_core.threads.models import LeaseRequest, ThreadChange, ThreadKey
from gc_stores.postgres import PostgresThreadStore
from gc_stores.sqlite import SQLiteThreadStore

KEY = ThreadKey(system="s", thread="big")
#: ~5 МБ в UTF-8: кириллица — два байта на символ.
BIG = {"role": "tool", "content": "я" * (5 * 1024 * 1024 // 2)}


@pytest.fixture(params=["sqlite", "postgres", "mysql", "redis", "s3"])
async def store(request, tmp_path):
    if request.param == "sqlite":
        made = SQLiteThreadStore(tmp_path / "big.db")
    elif request.param == "redis":
        from gc_stores.redis import RedisThreadStore

        url = request.getfixturevalue("redis_url")
        prefix = request.getfixturevalue("redis_prefixes").new()
        made = RedisThreadStore(url, prefix=prefix)
    elif request.param == "s3":
        from gc_stores.s3 import S3ThreadStore

        config = request.getfixturevalue("s3_config")
        made = S3ThreadStore(
            config["bucket"],
            prefix=request.getfixturevalue("s3_prefixes").new(),
            endpoint_url=config["endpoint_url"],
            region=config["region"],
        )
    elif request.param == "mysql":
        from gc_stores.mysql import MySQLThreadStore

        databases = request.getfixturevalue("mysql_databases")
        made = MySQLThreadStore(await databases.acreate())
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


#: Вложение на 20 МБ — больше предела строки результата драйвера MySQL
#: (16 МиБ).
HUGE = b"\x89PNG" + b"\0" * (20 * 1024 * 1024)


async def test_twenty_megabyte_blob_round_trips_or_is_refused(store):
    """Блоб на 20 МБ читается обратно целиком или отвергается при записи
    понятной ошибкой — и хранилище после этого работает (ревью, R3:
    на MySQL блоб записывался, не читался и ломал соединение пула)."""
    record = await store.claim(
        KEY, LeaseRequest(owner="w", ttl=timedelta(minutes=1))
    )
    try:
        ref = await store.put_blob(KEY, HUGE, mime="image/png")
    except ValueError as exc:
        assert "МиБ" in str(exc)
    else:
        assert await store.get_blob(ref) == HUGE
    for _ in range(12):  # больше, чем соединений в пуле
        assert (await store.get_thread(KEY)).lease == record.lease
