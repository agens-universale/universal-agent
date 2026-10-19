"""Хранилища на Postgres: ``PostgresThreadStore``, ``PostgresAgentStore``.

Драйвер — asyncpg (extra ``gc-stores[postgres]``); импорт модуля его не
требует, отказ без драйвера — при первом подключении.
"""

from gc_stores.postgres.store import PostgresAgentStore, PostgresThreadStore

__all__ = ["PostgresAgentStore", "PostgresThreadStore"]
