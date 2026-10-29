"""Утилита ``gc-stores``: обслуживание хранилищ на базах.

``gc-stores migrate --store postgres --dsn …`` — применить миграции схемы;
``--check`` — только сверить версию (код выхода 1 — не совпадает). С
``--config`` — схема каждой части ``storage`` в базе (треды и агент; одна
база на обе — один раз).

``gc-stores sync <каталог> --store … --agent-id coder [--project …]`` —
разложить каталог агента (промпты, AGENTS.md, субагенты, скиллы) в
хранилище агента; ``--with-defaults`` — и умолчания пакета gc-core
(после обновления gc-ua — заново; без каталога — только они),
``--prune`` — убрать лишнее, ``--dry-run`` — только отчёт.

``gc-stores maintain --store …`` — обслуживание хранилища тредов: квитанции
старше окна идемпотентности, надгробия старше суток, сироты прерванных
записей (Redis, S3). ``gc-serve`` делает это сам раз в час; без него (gc-cli
и TUI на базе) — эту команду, например из cron.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

from gc_stores import migrations
from gc_stores.configured import StorageConfigError
from gc_stores.connect import (
    UsageError,
    add_connection_args,
    open_agent_store,
    open_schema_parts,
    open_thread_store,
)
from gc_stores.sync import SyncReport, copy_defaults, sync_directory


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
    sync.add_argument(
        "directory",
        type=Path,
        nargs="?",
        help="каталог агента (без него — только --with-defaults)",
    )
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
    maintain = commands.add_parser(
        "maintain",
        help="убрать старые квитанции, надгробия и сироты хранилища тредов",
    )
    add_connection_args(maintain)
    maintain.add_argument(
        "--receipts-hours",
        type=float,
        default=24.0,
        help="окно идемпотентности: квитанции старше — убрать (24)",
    )
    return parser


async def _migrate(args: argparse.Namespace) -> int:
    """Схема базы; с ``--config`` — каждой части ``storage`` в базе (база
    агента отдельная — и её)."""
    if not args.config:
        return await _migrate_one("", open_thread_store(args), args.check)
    code = 0
    for part, store in open_schema_parts(args):
        code = max(code, await _migrate_one(f"{part}: ", store, args.check))
    return code


async def _migrate_one(label: str, store, check: bool) -> int:
    try:
        if check:
            try:
                await store.check_schema()
            except migrations.SchemaMismatch as exc:
                print(f"gc-stores: {label}{exc}", file=sys.stderr)
                return 1
        else:
            await store.ensure_schema()
        version = getattr(store, "schema_version", None)
        print(
            f"{label}версия схемы: {await version()}"
            if version is not None
            else f"{label}схема в порядке"
        )
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
    if args.directory is None and not args.with_defaults:
        raise UsageError("нужен каталог агента или --with-defaults")
    store = open_agent_store(args)
    try:
        report = (
            await copy_defaults(store, dry_run=args.dry_run)
            if args.directory is None
            else await sync_directory(
                store,
                args.directory,
                project=args.project,
                with_defaults=args.with_defaults,
                prune=args.prune,
                dry_run=args.dry_run,
            )
        )
    except ValueError as exc:
        print(f"gc-stores: {exc}", file=sys.stderr)
        return 1
    finally:
        await store.close()
    _print_report(report, dry_run=args.dry_run)
    return 0


async def _maintain(args: argparse.Namespace) -> int:
    store = open_thread_store(args)
    try:
        receipts = await store.prune_receipts(
            timedelta(hours=args.receipts_hours)
        )
        sweep = getattr(store, "sweep", None)
        orphans = await sweep() if callable(sweep) else 0
    finally:
        await store.close()
    print(f"квитанций убрано: {receipts}, сирот: {orphans}")
    return 0


_COMMANDS = {"migrate": _migrate, "sync": _sync, "maintain": _maintain}


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
