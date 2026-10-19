"""Утилита ``gc-stores``: обслуживание хранилищ на базах.

``gc-stores migrate --store postgres --dsn …`` — применить миграции схемы;
``--check`` — только сверить версию (код выхода 1 — не совпадает).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from gc_stores import migrations
from gc_stores.connect import (
    UsageError,
    add_connection_args,
    open_thread_store,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gc-stores", description="Хранилища GigaCode на базах"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser(
        "migrate", help="применить миграции схемы базы"
    )
    add_connection_args(migrate)
    migrate.add_argument(
        "--check",
        action="store_true",
        help="только сверить версию схемы с кодом",
    )
    return parser


async def _migrate(args: argparse.Namespace) -> int:
    store = open_thread_store(args)
    try:
        if args.check:
            try:
                await store.check_schema()
            except migrations.SchemaMismatch as exc:
                print(f"gc-stores: {exc}", file=sys.stderr)
                return 1
        else:
            await store.ensure_schema()
        print(f"версия схемы: {await store.schema_version()}")
        return 0
    finally:
        await store.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_migrate(args))
    except UsageError as exc:
        print(f"gc-stores: {exc}", file=sys.stderr)
        return 2
