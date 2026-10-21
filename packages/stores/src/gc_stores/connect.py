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
    """Как открыть хранилище тредов (схема) и хранилище агента."""

    threads: Callable[[argparse.Namespace], Any]
    agent: Callable[[argparse.Namespace], Any]


def _value(args: argparse.Namespace, flag: str, env: str) -> str:
    value = getattr(args, flag.lstrip("-").replace("-", "_"), None)
    value = value or os.environ.get(env, "")
    if not value:
        raise UsageError(f"нужен {flag} (или переменная {env})")
    return value


def _agent_id(args: argparse.Namespace) -> str:
    agent_id = getattr(args, "agent_id", None)
    if not agent_id:
        raise UsageError("нужен --agent-id: ресурсы агента в базе по нему")
    return agent_id


def _postgres(args: argparse.Namespace) -> Any:
    from gc_stores.postgres import PostgresThreadStore

    return PostgresThreadStore(
        _value(args, "--dsn", "GC_STORES_DSN"), schema=args.schema
    )


def _postgres_agent(args: argparse.Namespace) -> Any:
    from gc_stores.postgres import PostgresAgentStore

    return PostgresAgentStore(
        _value(args, "--dsn", "GC_STORES_DSN"),
        schema=args.schema,
        agent_id=_agent_id(args),
    )


def _mysql(args: argparse.Namespace) -> Any:
    from gc_stores.mysql import MySQLThreadStore

    return MySQLThreadStore(_value(args, "--dsn", "GC_STORES_DSN"))


def _mysql_agent(args: argparse.Namespace) -> Any:
    from gc_stores.mysql import MySQLAgentStore

    return MySQLAgentStore(
        _value(args, "--dsn", "GC_STORES_DSN"), agent_id=_agent_id(args)
    )


def _redis(args: argparse.Namespace) -> Any:
    from gc_stores.redis import RedisThreadStore

    return RedisThreadStore(
        _value(args, "--url", "GC_STORES_REDIS_URL"), prefix=args.prefix
    )


def _redis_agent(args: argparse.Namespace) -> Any:
    from gc_stores.redis import RedisAgentStore

    return RedisAgentStore(
        _value(args, "--url", "GC_STORES_REDIS_URL"),
        prefix=args.prefix,
        agent_id=_agent_id(args),
    )


def _s3_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "prefix": "" if args.prefix == "gc:" else args.prefix,
        "endpoint_url": args.endpoint_url
        or os.environ.get("GC_STORES_S3_ENDPOINT")
        or None,
        "region": args.region,
    }


def _s3(args: argparse.Namespace) -> Any:
    from gc_stores.s3 import S3ThreadStore

    return S3ThreadStore(
        _value(args, "--bucket", "GC_STORES_S3_BUCKET"), **_s3_options(args)
    )


def _s3_agent(args: argparse.Namespace) -> Any:
    from gc_stores.s3 import S3AgentStore

    return S3AgentStore(
        _value(args, "--bucket", "GC_STORES_S3_BUCKET"),
        agent_id=_agent_id(args),
        **_s3_options(args),
    )


def _sqlite(args: argparse.Namespace) -> Any:
    from gc_stores.sqlite import SQLiteThreadStore

    return SQLiteThreadStore(_value(args, "--path", "GC_STORES_SQLITE_PATH"))


def _sqlite_agent(args: argparse.Namespace) -> Any:
    from gc_stores.sqlite import SQLiteAgentStore

    return SQLiteAgentStore(
        _value(args, "--path", "GC_STORES_SQLITE_PATH"),
        agent_id=_agent_id(args),
    )


STORES: dict[str, StoreKind] = {
    "mysql": StoreKind(threads=_mysql, agent=_mysql_agent),
    "postgres": StoreKind(threads=_postgres, agent=_postgres_agent),
    "redis": StoreKind(threads=_redis, agent=_redis_agent),
    "s3": StoreKind(threads=_s3, agent=_s3_agent),
    "sqlite": StoreKind(threads=_sqlite, agent=_sqlite_agent),
}


def add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", choices=sorted(STORES))
    parser.add_argument(
        "--config",
        help="config.yml агента: подключение — из его секции storage",
    )
    parser.add_argument("--profile", help="профиль config.yml")
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


def _configured(args: argparse.Namespace) -> Any:
    from gc_core.config import load_settings

    return load_settings(args.config, profile_override=args.profile)


def _need_store(args: argparse.Namespace) -> str:
    if not args.store:
        raise UsageError("нужен --store или --config (секция storage)")
    return args.store


def open_thread_store(args: argparse.Namespace) -> Any:
    if args.config:
        from gc_stores.configured import open_part

        return open_part(_configured(args), "threads")
    return STORES[_need_store(args)].threads(args)


def open_schema_parts(args: argparse.Namespace) -> list[tuple[str, Any]]:
    """Хранилища со схемой из ``storage`` config.yml (``--config``): части
    ``threads`` и ``agent`` в базах, одна база на две части — один раз.
    Обе части — файлы: ``StorageConfigError``."""
    from gc_core.config_modules.storage import FILES
    from gc_core.settings import settings_of

    from gc_stores.configured import StorageConfigError, open_part

    settings = _configured(args)
    storage = settings_of(settings).storage
    threads = storage.part("threads")
    kind, params = storage.part("agent")
    params.pop("agent_id", None)
    wanted = []
    if threads[0] != FILES:
        wanted.append("threads")
    if kind != FILES and (kind, params) != threads:
        wanted.append("agent")
    if not wanted:
        raise StorageConfigError(
            "в storage config.yml обе части — files: схемы у файлов нет"
        )
    return [(part, open_part(settings, part)) for part in wanted]


def open_agent_store(args: argparse.Namespace) -> Any:
    if args.config:
        from gc_stores.configured import open_part

        return open_part(
            _configured(args),
            "agent",
            agent_id=getattr(args, "agent_id", None),
        )
    return STORES[_need_store(args)].agent(args)
