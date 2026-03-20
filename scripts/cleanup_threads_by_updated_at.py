#!/usr/bin/env python3
import argparse
import json
import os
from datetime import datetime, timedelta
from typing import Any, Optional


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
    if base.startswith("meta_") and base.endswith(".json"):
        tail = base[len("meta_") : -len(".json")]
        return os.path.join(os.path.dirname(meta_path), f"data_{tail}.jsonl")

    # На случай неожиданного формата оставим "как есть".
    return meta_path + ".l"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Удалить треды старше заданного срока по updated_at."
    )
    parser.add_argument(
        "--threads-dir",
        default=None,
        help="Каталог с файлами threads (meta_*.json / data_*.jsonl).",
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

    # По умолчанию: <project_root>/threads
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    threads_dir = (
        os.path.abspath(args.threads_dir)
        if args.threads_dir
        else os.path.join(project_root, "threads")
    )

    if not os.path.isdir(threads_dir):
        print(f"[cleanup] threads_dir not found: {threads_dir}")
        return 0

    now = datetime.now().astimezone()
    cutoff = now - timedelta(days=args.ttl_days)

    meta_paths = [
        os.path.join(threads_dir, name)
        for name in os.listdir(threads_dir)
        if name.startswith("meta_") and name.endswith(".json")
    ]

    scanned = 0
    removed_meta = 0
    removed_data = 0
    skipped = 0
    errors = 0

    for meta_path in sorted(meta_paths):
        scanned += 1
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            if not isinstance(meta, dict):
                skipped += 1
                continue

            dt = _parse_datetime_maybe(meta.get("updated_at"))
            if dt is None:
                dt = _parse_datetime_maybe(meta.get("created_at"))
            if dt is None:
                skipped += 1
                continue

            # Приведем к timezone-aware (если из файла пришло naive-зн. — считаем локальным).
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=now.tzinfo)

            if dt >= cutoff:
                skipped += 1
                continue

            data_path = _meta_to_data_path(meta_path)

            if args.dry_run:
                removed_meta += 1
                if os.path.exists(data_path):
                    removed_data += 1
                continue

            try:
                os.remove(meta_path)
                removed_meta += 1
            except FileNotFoundError:
                pass

            try:
                if os.path.exists(data_path):
                    os.remove(data_path)
                    removed_data += 1
            except FileNotFoundError:
                pass
        except Exception as e:
            errors += 1
            print(f"[cleanup] error for {meta_path}: {e!r}")

    print(
        "[cleanup] done",
        {
            "threads_dir": threads_dir,
            "ttl_days": args.ttl_days,
            "scanned": scanned,
            "skipped": skipped,
            "removed_meta": removed_meta,
            "removed_data": removed_data,
            "errors": errors,
        },
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

