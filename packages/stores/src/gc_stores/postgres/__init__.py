"""Хранилище тредов на Postgres: ``PostgresThreadStore``.

Драйвер — asyncpg (extra ``gc-stores[postgres]``); импорт модуля его не
требует, отказ без драйвера — при первом подключении.
"""

from gc_stores.postgres.store import PostgresThreadStore

__all__ = ["PostgresThreadStore"]
