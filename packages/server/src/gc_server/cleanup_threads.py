#!/usr/bin/env python3
import argparse
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from gc_core.threads import file_layout
from gc_core.threads.errors import OwnershipError
from gc_core.threads.facade import Threads, run_sync
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadRecord

#: Квитанции операций нужны только на время повторов записи.
_RECEIPTS_TTL = timedelta(days=7)


def _parse_datetime_maybe(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        # ISO-8601: datetime.fromisoformat() понимает и смещения (+00:00).
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _is_stale(record: ThreadRecord, now: datetime, cutoff: datetime) -> bool:
    moment = _parse_datetime_maybe(
        record.info.updated_at
    ) or _parse_datetime_maybe(record.info.created_at)
    if moment is None:
        return False
    # Приведём к timezone-aware: naive-значение считаем локальным.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=now.tzinfo)
    return moment < cutoff


async def _all_threads(threads: Threads) -> list[ThreadRecord]:
    return [record async for record in threads.iter_threads()]


def _delete_stale(
    threads: Threads,
    store: FileThreadStore,
    record: ThreadRecord,
    dry_run: bool,
    result: dict,
) -> None:
    """Тред целиком — тем же путём, что удаление сессии в TUI: вместе с
    картинками, трассой субагентов, архивами сжатия и журналом правок."""
    key = record.key
    # Счётчик develop — файлы data, а не сообщения (data бывает и без них).
    has_data = Path(
        file_layout.history_file(store.threads_dir, key.system, key.thread)
    ).exists()
    if not dry_run:
        try:
            run_sync(threads.delete(key))
        except OwnershipError:
            # Тред ведёт durable-исполнитель (Temporal): не трогаем.
            result["skipped"] += 1
            return
    result["removed_meta"] += 1
    result["removed_data"] += int(has_data)


def cleanup_threads(
    threads_dir: str,
    ttl_days: int = 7,
    *,
    dry_run: bool = False,
) -> dict:
    """Удалить треды, не обновлявшиеся дольше ``ttl_days``. Вернуть статистику.

    Треды — через фасад ``Threads`` (удаление целиком); устаревшие архивы
    сжатия и meta старых форматов — обслуживанием файлового хранилища
    (``sweep_orphans``); квитанции записи старше недели — ``prune``.
    """
    result = {
        "threads_dir": threads_dir,
        "ttl_days": ttl_days,
        "scanned": 0,
        "skipped": 0,
        "removed_meta": 0,
        "removed_data": 0,
        "errors": 0,
    }
    if not Path(threads_dir).is_dir():
        result["missing_dir"] = True
        return result

    now = datetime.now().astimezone()
    cutoff = now - timedelta(days=ttl_days)
    store = FileThreadStore(threads_dir)
    threads = Threads(store)

    for record in run_sync(_all_threads(threads)):
        result["scanned"] += 1
        if not _is_stale(record, now, cutoff):
            result["skipped"] += 1
            continue
        try:
            _delete_stale(threads, store, record, dry_run, result)
        except Exception as e:
            result["errors"] += 1
            print(f"[cleanup] error for {record.key}: {e!r}")

    removed_meta, removed_data = store.sweep_orphans(cutoff, dry_run=dry_run)
    result["removed_meta"] += removed_meta
    result["removed_data"] += removed_data
    if not dry_run:
        result["pruned_receipts"] = run_sync(threads.prune(_RECEIPTS_TTL))
    return result


def _default_threads_dir() -> str:
    """Тот же каталог, который чистит фоновый janitor сервера.

    Импорт локальный: модуль обязан оставаться запускаемым как
    ``python -m gc_server.cleanup_threads`` и без разбора конфига, если каталог
    задан явно.
    """
    from gc_core.thread_store import resolve_threads_dir

    from gc_server.rest_api.server_config import (
        build_agent_settings_for_server,
        load_server_config,
    )

    return resolve_threads_dir(
        build_agent_settings_for_server(load_server_config())
    )


def main() -> int:
    """CLI-обёртка: `python -m gc_server.cleanup_threads`.

    Дефолт ``--threads-dir`` берётся ОТТУДА ЖЕ, откуда его берёт фоновый janitor
    (``server_cleanup_threads.register_cleanup_threads_on_startup``) — из
    серверного конфига через ``resolve_threads_dir``. Иначе одна и та же чистка
    имела бы два разных представления о том, что чистит.

    Пока чистка лежала в ``scripts/``, дефолт считался от файла скрипта
    (``<script>/../threads``). Переезд внутрь пакета этот якорь обессмыслил, и на
    его месте оказалось голое ``"threads"`` относительно CWD: в cron с чужим CWD
    команда печатала «not found» и возвращала 0 — тихий успех, при котором треды
    копились месяцами. Восстанавливать прежний якорь незачем: каталога
    ``<репо>/threads`` не существовало никогда, ни в дереве, ни в истории — это
    был рудимент.

    От CWD дефолт по-прежнему зависит, и это НЕ дефект, а модель продукта:
    ``project_slug()`` даёт каждому рабочему каталогу свой стор тредов, ровно как
    у CLI и TUI. Поэтому cron надо ставить с CWD проекта либо передавать
    ``--threads-dir`` явно; выбранный путь печатается в выводе абсолютным.
    """
    parser = argparse.ArgumentParser(
        description="Удалить треды старше заданного срока по updated_at."
    )
    parser.add_argument(
        "--threads-dir",
        default=None,
        help=(
            "Каталог с файлами threads (meta_*.json / data_*.jsonl). По умолчанию — тот же, что чистит janitor сервера."
        ),
    )
    parser.add_argument(
        "--ttl-days",
        type=int,
        default=7,
        help="Удалять треды, обновленные раньше чем сейчас - ttl-days.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Не удалять файлы, только посчитать что будет удалено.",
    )
    args = parser.parse_args()

    # Абсолютный путь в любом случае: раньше в статистику уходил аргумент как
    # есть, и по строке «threads» нельзя было понять, какой каталог смотрели.
    threads_dir = (
        os.path.abspath(args.threads_dir)
        if args.threads_dir
        else _default_threads_dir()
    )
    stats = cleanup_threads(threads_dir, args.ttl_days, dry_run=args.dry_run)
    if stats.get("missing_dir"):
        print(f"[cleanup] threads_dir not found: {threads_dir}")
        return 0
    print("[cleanup] done", stats)
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
