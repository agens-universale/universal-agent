"""``PostgresThreadStore`` проходит конформанс контракта v2 целиком, а порт
сессии поверх него (путь, которым ходит агент) — старый асинхронный набор."""

from gc_core.threads.session_port import SessionPort
from gc_stores.postgres import PostgresThreadStore
from gc_testkit.thread_store_conformance import assert_conforms_async
from gc_testkit.thread_store_conformance_v2 import CHECKS, assert_conforms


def test_postgres_store_conforms(postgres_dsn, schemas):
    def make(clock):
        schema = schemas.create()
        return PostgresThreadStore(postgres_dsn, schema=schema, clock=clock)

    assert assert_conforms(make) == [check.__name__ for check in CHECKS]


class _ClosingPort(SessionPort):
    """Порт, который набор может закрыть: пул базы живёт в loop набора."""

    async def close(self) -> None:
        await self._store.close()


def test_session_port_over_postgres_keeps_old_contract(postgres_dsn, schemas):
    def factory():
        schema = schemas.create()
        return _ClosingPort(PostgresThreadStore(postgres_dsn, schema=schema))

    assert len(assert_conforms_async(factory)) == 18
