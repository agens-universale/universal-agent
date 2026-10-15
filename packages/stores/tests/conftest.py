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
