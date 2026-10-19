"""Сборка хранилища по флагам утилиты ``gc-stores`` и переменным окружения.

Секреты — не в истории оболочки: адрес базы берётся из флага или из
переменной (``GC_STORES_DSN``, ``GC_STORES_REDIS_URL``,
``GC_STORES_S3_*``). Реестр ``STORES`` дополняют подпакеты баз.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class UsageError(ValueError):
    """Не хватает флага подключения: код выхода 2, как у argparse."""


@dataclass(frozen=True)
class StoreKind:
    """Как открыть хранилище тредов (для схемы) и хранилище агента."""

    threads: Callable[[argparse.Namespace], Any]


def _value(args: argparse.Namespace, flag: str, env: str) -> str:
    value = getattr(args, flag.lstrip("-").replace("-", "_"), None)
    value = value or os.environ.get(env, "")
    if not value:
        raise UsageError(f"нужен {flag} (или переменная {env})")
    return value


def _postgres(args: argparse.Namespace) -> Any:
    from gc_stores.postgres import PostgresThreadStore

    return PostgresThreadStore(
        _value(args, "--dsn", "GC_STORES_DSN"), schema=args.schema
    )


def _mysql(args: argparse.Namespace) -> Any:
    from gc_stores.mysql import MySQLThreadStore

    return MySQLThreadStore(_value(args, "--dsn", "GC_STORES_DSN"))


def _redis(args: argparse.Namespace) -> Any:
    from gc_stores.redis import RedisThreadStore

    return RedisThreadStore(
        _value(args, "--url", "GC_STORES_REDIS_URL"), prefix=args.prefix
    )


def _s3(args: argparse.Namespace) -> Any:
    from gc_stores.s3 import S3ThreadStore

    return S3ThreadStore(
        _value(args, "--bucket", "GC_STORES_S3_BUCKET"),
        prefix="" if args.prefix == "gc:" else args.prefix,
        endpoint_url=args.endpoint_url
        or os.environ.get("GC_STORES_S3_ENDPOINT")
        or None,
        region=args.region,
    )


def _sqlite(args: argparse.Namespace) -> Any:
    from gc_stores.sqlite import SQLiteThreadStore

    return SQLiteThreadStore(_value(args, "--path", "GC_STORES_SQLITE_PATH"))


STORES: dict[str, StoreKind] = {
    "mysql": StoreKind(threads=_mysql),
    "postgres": StoreKind(threads=_postgres),
    "redis": StoreKind(threads=_redis),
    "s3": StoreKind(threads=_s3),
    "sqlite": StoreKind(threads=_sqlite),
}


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", required=True, choices=sorted(STORES))
    parser.add_argument("--dsn", help="адрес базы (или GC_STORES_DSN)")
    parser.add_argument(
        "--schema", default="public", help="схема Postgres (public)"
    )
    parser.add_argument(
        "--path", help="файл SQLite (или GC_STORES_SQLITE_PATH)"
    )
    parser.add_argument("--url", help="адрес Redis (или GC_STORES_REDIS_URL)")
    parser.add_argument(
        "--prefix",
        default="gc:",
        help="префикс ключей: Redis (gc:), S3 (без префикса)",
    )
    parser.add_argument("--bucket", help="бакет S3 (или GC_STORES_S3_BUCKET)")
    parser.add_argument(
        "--endpoint-url", help="S3-совместимое (или GC_STORES_S3_ENDPOINT)"
    )
    parser.add_argument("--region", help="регион S3")


def open_thread_store(args: argparse.Namespace) -> Any:
    return STORES[args.store].threads(args)
