"""``PostgresThreadStore`` проходит конформанс контракта v2 целиком и набор
пути хода (сессия агента поверх него)."""

import asyncio

from gc_stores.postgres import PostgresThreadStore
from gc_testkit.session_checks import CHECKS as SESSION_CHECKS
from gc_testkit.session_checks import aassert_session_works
from gc_testkit.thread_store_conformance_v2 import CHECKS, assert_conforms


def test_postgres_store_conforms(postgres_dsn, schemas):
    def make(clock):
        schema = schemas.create()
        return PostgresThreadStore(postgres_dsn, schema=schema, clock=clock)

    assert assert_conforms(make) == [check.__name__ for check in CHECKS]


def test_session_over_postgres_works(postgres_dsn, schemas):
    # Схемы — до цикла набора: фикстура создаёт их своим asyncio.run.
    names = iter([schemas.create() for _ in SESSION_CHECKS])

    def make():
        return PostgresThreadStore(postgres_dsn, schema=next(names))

    passed = asyncio.run(aassert_session_works(make))
    assert passed == [check.__name__ for check in SESSION_CHECKS]
