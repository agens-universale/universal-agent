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
    from gc_testkit import scripted_model

    return scripted_model.install(monkeypatch)


REDIS_ENV = "GC_TEST_REDIS_URL"


@pytest.fixture(scope="session")
def redis_url() -> str:
    url = os.environ.get(REDIS_ENV, "")
    if url:
        return url
    if os.environ.get("REQUIRE_REDIS") == "1":
        pytest.fail(f"REQUIRE_REDIS=1, но {REDIS_ENV} не задан")
    pytest.skip(f"{REDIS_ENV} не задан: тесты Redis пропущены")


class RedisPrefixes:
    """Префикс ключей на хранилище теста; ``drop_all`` удаляет ключи."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.made: list[str] = []

    def new(self) -> str:
        prefix = f"gc_test_{uuid.uuid4().hex[:12]}:"
        self.made.append(prefix)
        return prefix

    def drop_all(self) -> None:
        async def drop():
            import redis.asyncio as aioredis

            client = aioredis.Redis.from_url(self.url)
            try:
                for prefix in self.made:
                    async for key in client.scan_iter(match=f"{prefix}*"):
                        await client.delete(key)
            finally:
                await client.aclose()

        asyncio.run(drop())


@pytest.fixture
def redis_prefixes(redis_url):
    made = RedisPrefixes(redis_url)
    yield made
    made.drop_all()


S3_ENV = "GC_TEST_S3_ENDPOINT"


@pytest.fixture(scope="session")
def s3_config() -> dict:
    """Бакет S3 для тестов: ``GC_TEST_S3_ENDPOINT``, ``GC_TEST_S3_BUCKET``,
    ключи — стандартные переменные AWS (``AWS_ACCESS_KEY_ID``…)."""
    endpoint = os.environ.get(S3_ENV, "")
    if not endpoint:
        if os.environ.get("REQUIRE_S3") == "1":
            pytest.fail(f"REQUIRE_S3=1, но {S3_ENV} не задан")
        pytest.skip(f"{S3_ENV} не задан: тесты S3 пропущены")
    return {
        "endpoint_url": endpoint,
        "bucket": os.environ.get("GC_TEST_S3_BUCKET", "gc-test"),
        "region": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    }


class S3Prefixes:
    """Префикс ключей на хранилище теста; ``drop_all`` удаляет объекты."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.made: list[str] = []

    def new(self) -> str:
        prefix = f"gc_test_{uuid.uuid4().hex[:12]}/"
        self.made.append(prefix)
        return prefix

    def drop_all(self) -> None:
        async def drop():
            from aiobotocore.session import get_session

            async with get_session().create_client(
                "s3",
                endpoint_url=self.config["endpoint_url"],
                region_name=self.config["region"],
            ) as s3:
                for prefix in self.made:
                    listed = await s3.list_objects_v2(
                        Bucket=self.config["bucket"], Prefix=prefix
                    )
                    for item in listed.get("Contents", []):
                        await s3.delete_object(
                            Bucket=self.config["bucket"], Key=item["Key"]
                        )

        asyncio.run(drop())


@pytest.fixture
def s3_prefixes(s3_config):
    made = S3Prefixes(s3_config)
    yield made
    made.drop_all()
