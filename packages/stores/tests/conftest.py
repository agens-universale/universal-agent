"""Тесты хранилищ на базах: Postgres берётся из ``GC_TEST_POSTGRES_DSN``.

Без переменной тесты с базой пропускаются; ``REQUIRE_POSTGRES=1`` (прогон,
который обязан проверить Postgres) делает её отсутствие ошибкой. Каждое
хранилище в тестах живёт на своей схеме — тесты не видят данных друг друга;
схемы удаляются в конце теста.
"""

import asyncio
import os
import uuid

import pytest
from gc_stores.postgres import PostgresThreadStore

DSN_ENV = "GC_TEST_POSTGRES_DSN"


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    dsn = os.environ.get(DSN_ENV, "")
    if dsn:
        return dsn
    if os.environ.get("REQUIRE_POSTGRES") == "1":
        pytest.fail(f"REQUIRE_POSTGRES=1, но {DSN_ENV} не задан")
    pytest.skip(f"{DSN_ENV} не задан: тесты Postgres пропущены")


class Schemas:
    """Свежие схемы с таблицами; ``drop_all`` удаляет созданные."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.created: list[str] = []

    def name(self) -> str:
        name = f"gc_test_{uuid.uuid4().hex[:12]}"
        self.created.append(name)
        return name

    async def acreate(self) -> str:
        name = self.name()
        async with PostgresThreadStore(self.dsn, schema=name) as store:
            await store.ensure_schema()
        return name

    def create(self) -> str:
        """Для синхронных тестов (конформанс сам крутит свой loop)."""
        return asyncio.run(self.acreate())

    async def _adrop_all(self) -> None:
        import asyncpg

        conn = await asyncpg.connect(self.dsn)
        try:
            for name in self.created:
                await conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        finally:
            await conn.close()

    def drop_all(self) -> None:
        asyncio.run(self._adrop_all())


@pytest.fixture
def schemas(postgres_dsn):
    made = Schemas(postgres_dsn)
    yield made
    made.drop_all()


@pytest.fixture
async def pg_schema(schemas) -> str:
    return await schemas.acreate()


@pytest.fixture
async def pg_store(postgres_dsn, pg_schema):
    async with PostgresThreadStore(postgres_dsn, schema=pg_schema) as store:
        yield store


MYSQL_ENV = "GC_TEST_MYSQL_DSN"


@pytest.fixture(scope="session")
def mysql_dsn() -> str:
    """Адрес сервера MySQL (база в пути — любая существующая)."""
    dsn = os.environ.get(MYSQL_ENV, "")
    if dsn:
        return dsn
    if os.environ.get("REQUIRE_MYSQL") == "1":
        pytest.fail(f"REQUIRE_MYSQL=1, но {MYSQL_ENV} не задан")
    pytest.skip(f"{MYSQL_ENV} не задан: тесты MySQL пропущены")


class MySQLDatabases:
    """Свежие базы MySQL на тест; ``drop_all`` удаляет созданные."""

    def __init__(self, dsn: str) -> None:
        from urllib.parse import urlsplit, urlunsplit

        self._split, self._join = urlsplit, urlunsplit
        self.dsn = dsn
        self.created: list[str] = []

    def dsn_of(self, name: str) -> str:
        parts = self._split(self.dsn)
        return self._join(parts._replace(path=f"/{name}"))

    async def _admin(self, statement: str) -> None:
        from gc_stores.mysql.database import driver, parse_dsn

        options = parse_dsn(self.dsn)
        conn = await driver().connect(
            host=options["host"],
            port=options["port"],
            user=options["user"],
            password=options["password"],
            autocommit=True,
        )
        try:
            async with conn.cursor() as cur:
                await cur.execute(statement)
        finally:
            await conn.ensure_closed()

    async def acreate(self, *, migrate: bool = True) -> str:
        from gc_stores.mysql import MySQLThreadStore

        name = f"gc_test_{uuid.uuid4().hex[:12]}"
        self.created.append(name)
        await self._admin(
            f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_0900_bin"
        )
        if migrate:
            async with MySQLThreadStore(self.dsn_of(name)) as store:
                await store.ensure_schema()
        return self.dsn_of(name)

    def create(self) -> str:
        return asyncio.run(self.acreate())

    def drop_all(self) -> None:
        async def drop():
            for name in self.created:
                await self._admin(f"DROP DATABASE IF EXISTS `{name}`")

        asyncio.run(drop())


@pytest.fixture
def mysql_databases(mysql_dsn):
    made = MySQLDatabases(mysql_dsn)
    yield made
    made.drop_all()


@pytest.fixture
async def mysql_store(mysql_databases):
    from gc_stores.mysql import MySQLThreadStore

    async with MySQLThreadStore(await mysql_databases.acreate()) as store:
        yield store


@pytest.fixture
def requests_seen(monkeypatch):
    """Скриптованная модель (``scripted_model``): запросы к ней по порядку."""
    import scripted_model

    return scripted_model.install(monkeypatch)
