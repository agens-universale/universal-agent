"""Схема баз — пронумерованные миграции и таблица версии: пустая база
получает все, старая или более новая — понятный отказ при старте."""

import asyncio

import pytest
from gc_core.threads.models import ThreadKey
from gc_stores import migrations
from gc_stores.migrations import SchemaMismatch
from gc_stores.postgres import PostgresThreadStore

KEY = ThreadKey(system="s", thread="t")
LATEST = migrations.latest(migrations.load("gc_stores.postgres"))


async def _set_version(dsn: str, schema: str, version: int) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            f'UPDATE "{schema}".gc_schema_version SET version = $1', version
        )
    finally:
        await conn.close()


def test_migration_numbers_have_no_gaps():
    found = migrations.load("gc_stores.postgres")
    assert [m.version for m in found] == list(range(1, len(found) + 1))
    assert all(m.statements() for m in found)


def test_gap_in_numbers_is_refused(tmp_path):
    with pytest.raises(ValueError, match="разрыв"):
        migrations.check_sequence([1, 3])


async def test_empty_database_gets_all_migrations(postgres_dsn, schemas):
    async with PostgresThreadStore(
        postgres_dsn, schema=schemas.name()
    ) as store:
        await store.ensure_schema()
        assert await store.schema_version() == LATEST
        await store.ensure_schema()  # повтор — без DDL и без ошибки
        assert await store.get_thread(KEY) is None


async def test_old_schema_is_refused_with_the_fix(postgres_dsn, schemas):
    name = await schemas.acreate()
    await _set_version(postgres_dsn, name, 0)
    async with PostgresThreadStore(postgres_dsn, schema=name) as store:
        with pytest.raises(SchemaMismatch, match="gc-stores migrate"):
            await store.get_thread(KEY)


async def test_newer_schema_is_refused(postgres_dsn, schemas):
    name = await schemas.acreate()
    await _set_version(postgres_dsn, name, LATEST + 1)
    async with PostgresThreadStore(postgres_dsn, schema=name) as store:
        with pytest.raises(SchemaMismatch, match="более новой"):
            await store.get_thread(KEY)
        with pytest.raises(SchemaMismatch, match="более новой"):
            await store.ensure_schema()


async def test_store_without_schema_names_the_fix(postgres_dsn, schemas):
    async with PostgresThreadStore(
        postgres_dsn, schema=schemas.name()
    ) as store:
        with pytest.raises(SchemaMismatch, match="ensure_schema"):
            await store.get_thread(KEY)


async def test_two_instances_migrate_once(postgres_dsn, schemas):
    name = schemas.name()
    first = PostgresThreadStore(postgres_dsn, schema=name)
    second = PostgresThreadStore(postgres_dsn, schema=name)
    try:
        await asyncio.gather(first.ensure_schema(), second.ensure_schema())
        assert await first.schema_version() == LATEST
    finally:
        await asyncio.gather(first.close(), second.close())
