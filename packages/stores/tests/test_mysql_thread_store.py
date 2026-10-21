"""``MySQLThreadStore``: конформанс контракта v2 на живом MySQL 8 и то, чем
MySQL отличается — длина ключей, коллация, миграции без транзакционного
DDL, драйвер по extra."""

import asyncio
from datetime import UTC, datetime

import pytest
from gc_core.threads.models import (
    LeaseRequest,
    SideChange,
    ThreadKey,
    ThreadQuery,
)
from gc_stores import migrations
from gc_stores.mysql import MySQLThreadStore
from gc_testkit.thread_store_conformance_v2 import CHECKS, assert_conforms
from gc_testkit.session_checks import CHECKS as SESSION_CHECKS
from gc_testkit.session_checks import aassert_session_works

LATEST = migrations.latest(migrations.load("gc_stores.mysql"))


def test_mysql_store_conforms(mysql_databases):
    def make(clock):
        return MySQLThreadStore(mysql_databases.create(), clock=clock)

    assert assert_conforms(make) == [check.__name__ for check in CHECKS]


def test_session_over_mysql_works(mysql_databases):
    # Базы — до цикла набора: фикстура создаёт их своим asyncio.run.
    dsns = iter([mysql_databases.create() for _ in SESSION_CHECKS])

    def make():
        return MySQLThreadStore(next(dsns))

    passed = asyncio.run(aassert_session_works(make))
    assert passed == [check.__name__ for check in SESSION_CHECKS]


async def test_long_thread_id_is_a_clear_error(mysql_store):
    with pytest.raises(ValueError, match="255"):
        await mysql_store.claim(
            ThreadKey(system="s", thread="t" * 300), LeaseRequest(owner="w")
        )


async def test_list_orders_by_codepoints(mysql_databases):
    """utf8mb4_0900_bin: «-» < «B» < «_» < «a» < «b» — порядок кодов, как у
    остальных хранилищ (коллация по умолчанию сочла бы «B» и «b» равными).
    Часы общие: при равном ``updated_at`` порядок задают ключи."""
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    threads = ["b", "B", "a", "_x", "-y"]
    dsn = await mysql_databases.acreate()
    async with MySQLThreadStore(dsn, clock=lambda: moment) as store:
        for thread in threads:
            await store.append_side(
                ThreadKey(system="s", thread=thread), SideChange(op_id=thread)
            )
        page = await store.list_threads(ThreadQuery(system="s"))
    assert [r.key.thread for r in page.items] == sorted(threads)


async def test_two_instances_migrate_once(mysql_databases):
    dsn = await mysql_databases.acreate(migrate=False)
    first, second = MySQLThreadStore(dsn), MySQLThreadStore(dsn)
    try:
        await asyncio.gather(first.ensure_schema(), second.ensure_schema())
        assert await first.schema_version() == LATEST
    finally:
        await asyncio.gather(first.close(), second.close())


async def test_store_without_schema_names_the_fix(mysql_databases):
    from gc_stores.migrations import SchemaMismatch

    dsn = await mysql_databases.acreate(migrate=False)
    async with MySQLThreadStore(dsn) as store:
        with pytest.raises(SchemaMismatch, match="ensure_schema"):
            await store.get_thread(ThreadKey(system="s", thread="t"))


def test_missing_driver_names_the_extra(monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *args, **kwargs):
        if name == "asyncmy" or name.startswith("asyncmy."):
            raise ImportError("нет asyncmy")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake)
    store = MySQLThreadStore("mysql://nobody@127.0.0.1:1/x")
    with pytest.raises(ImportError, match=r"gc-stores\[mysql\]"):
        asyncio.run(store.get_thread(ThreadKey(system="s", thread="t")))


def test_dsn_requires_a_database():
    with pytest.raises(ValueError, match="mysql://"):
        MySQLThreadStore("mysql://root@127.0.0.1:3306")


async def test_trailing_spaces_are_different_keys(mysql_store):
    """Находка проверки безопасности: коллация с PAD SPACE считала «a» и
    «a » одним ключом — чужой алиас с хвостовым пробелом занимал алиас
    треда, а op_id с пробелом выдавал себя за другую операцию."""
    first = ThreadKey(system="s", thread="one")
    second = ThreadKey(system="s", thread="two")
    await mysql_store.append_side(
        first, SideChange(op_id="op", add_aliases=["dup"])
    )
    await mysql_store.append_side(
        second, SideChange(op_id="op ", add_aliases=["dup "])
    )
    assert await mysql_store.resolve_alias("dup") == first
    assert await mysql_store.resolve_alias("dup ") == second
