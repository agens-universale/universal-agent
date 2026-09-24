#!/usr/bin/env python3
import argparse
import json
import os
from datetime import datetime, timedelta
from typing import Any, Optional

# Константы вместо повторяющихся литералов (Sonar S1192).
_EXT_JSON = ".json"


def _parse_datetime_maybe(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    try:
        # ISO-8601: datetime.fromisoformat() понимает и смещения (+00:00).
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _meta_to_data_path(meta_path: str) -> str:
    """
    Конвертируем имя:
      threads/meta_<tail>.json -> threads/data_<tail>.jsonl
    """
    base = os.path.basename(meta_path)
    if base.startswith("meta_") and base.endswith(_EXT_JSON):
        tail = base[len("meta_") : -len(_EXT_JSON)]
        return os.path.join(os.path.dirname(meta_path), f"data_{tail}.jsonl")

    # На случай неожиданного формата оставим "как есть".
    return meta_path + ".l"


def _cleanup_thread(
    meta_path: str,
    now: datetime,
    cutoff: datetime,
    dry_run: bool,
    result: dict,
) -> None:
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    if not isinstance(meta, dict):
        result["skipped"] += 1
        return

    dt = _parse_datetime_maybe(meta.get("updated_at"))
    if dt is None:
        dt = _parse_datetime_maybe(meta.get("created_at"))
    if dt is None:
        result["skipped"] += 1
        return

    # Приведём к timezone-aware: naive-значение считаем локальным.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)

    if dt >= cutoff:
        result["skipped"] += 1
        return

    data_path = _meta_to_data_path(meta_path)

    if dry_run:
        result["removed_meta"] += 1
        if os.path.exists(data_path):
            result["removed_data"] += 1
        return

    try:
        os.remove(meta_path)
        result["removed_meta"] += 1
    except FileNotFoundError:
        pass

    try:
        if os.path.exists(data_path):
            os.remove(data_path)
            result["removed_data"] += 1
    except FileNotFoundError:
        pass


def cleanup_threads(
    threads_dir: str,
    ttl_days: int = 7,
    *,
    dry_run: bool = False,
) -> dict:
    """Удалить треды, не обновлявшиеся дольше ``ttl_days``. Вернуть статистику.

    Была частью ``main()`` этого файла, когда он лежал в ``scripts/`` и
    вызывался фоновым janitor'ом через ``subprocess``. Путь к скрипту считался
    от корня репозитория, а сам ``scripts/`` не попадал ни в одно колесо —
    в установленном профиле чистка не работала в принципе. Теперь это обычная
    функция пакета, а CLI ниже — тонкая обёртка над ней.
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
    if not os.path.isdir(threads_dir):
        result["missing_dir"] = True
        return result

    now = datetime.now().astimezone()
    cutoff = now - timedelta(days=ttl_days)

    meta_paths = [
        os.path.join(threads_dir, name)
        for name in os.listdir(threads_dir)
        if name.startswith("meta_") and name.endswith(_EXT_JSON)
    ]

    for meta_path in sorted(meta_paths):
        result["scanned"] += 1
        try:
            _cleanup_thread(meta_path, now, cutoff, dry_run, result)
        except Exception as e:
            result["errors"] += 1
            print(f"[cleanup] error for {meta_path}: {e!r}")

    return result


def _default_threads_dir() -> str:
    """Тот же каталог, который чистит фоновый janitor сервера.

    Импорт локальный: модуль обязан оставаться запускаемым как
    ``python -m gc_server.cleanup_threads`` и без разбора конфига, если каталог
    задан явно.
    """
    from gc_core.thread_store import resolve_threads_dir

    from gc_server.rest_api.server_config import (
        build_agent_config_for_server,
        load_server_config,
    )

    return resolve_threads_dir(
        build_agent_config_for_server(load_server_config())
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
