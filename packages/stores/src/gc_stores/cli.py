"""Утилита ``gc-stores``: обслуживание хранилищ на базах.

``gc-stores migrate --store postgres --dsn …`` — применить миграции схемы;
``--check`` — только сверить версию (код выхода 1 — не совпадает).

``gc-stores sync <каталог> --store … --agent-id coder [--project …]`` —
разложить каталог агента (промпты, AGENTS.md, субагенты, скиллы) в
хранилище агента; ``--with-defaults`` — и умолчания пакета gc-core
(после обновления gc-ua — заново), ``--prune`` — убрать лишнее,
``--dry-run`` — только отчёт.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from gc_stores import migrations
from gc_stores.configured import StorageConfigError
from gc_stores.connect import (
    UsageError,
    add_connection_args,
    open_agent_store,
    open_thread_store,
)
from gc_stores.sync import SyncReport, sync_directory


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
    sync = commands.add_parser(
        "sync", help="разложить каталог агента в хранилище агента"
    )
    sync.add_argument("directory", type=Path, help="каталог агента")
    add_connection_args(sync)
    sync.add_argument("--agent-id", help="имя агента в хранилище")
    sync.add_argument("--project", help="id проекта: L2, L3, скиллы project")
    sync.add_argument(
        "--with-defaults",
        action="store_true",
        help="и промпты/скиллы по умолчанию из пакета gc-core",
    )
    sync.add_argument(
        "--prune", action="store_true", help="убрать то, чего нет в каталоге"
    )
    sync.add_argument(
        "--dry-run", action="store_true", help="только показать, что будет"
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


def _print_report(report: SyncReport, *, dry_run: bool) -> None:
    verb = "будет записано" if dry_run else "записано"
    for name in report.written:
        print(f"+ {name}")
    for name in report.deleted:
        print(f"- {name}")
    print(
        f"{verb}: {len(report.written)}, без изменений: "
        f"{len(report.unchanged)}, удалено: {len(report.deleted)}"
    )


async def _sync(args: argparse.Namespace) -> int:
    store = open_agent_store(args)
    try:
        report = await sync_directory(
            store,
            args.directory,
            project=args.project,
            with_defaults=args.with_defaults,
            prune=args.prune,
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        print(f"gc-stores: {exc}", file=sys.stderr)
        return 1
    finally:
        await store.close()
    _print_report(report, dry_run=args.dry_run)
    return 0


_COMMANDS = {"migrate": _migrate, "sync": _sync}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_COMMANDS[args.command](args))
    except UsageError as exc:
        print(f"gc-stores: {exc}", file=sys.stderr)
        return 2
    except StorageConfigError as exc:
        print(f"gc-stores: {exc}", file=sys.stderr)
        return 1
