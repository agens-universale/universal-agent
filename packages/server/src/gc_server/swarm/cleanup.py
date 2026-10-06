#!/usr/bin/env python3
"""TTL-janitor для swarm-режима (шаг 3.1).

Удаляет каталоги swarm_threads/<id>/ старше TTL и, отдельным проходом,
sandbox-каталоги старше TTL. Можно запускать вручную или из cron /
startup-loop (шаг 3.2).

Треды и песочницы расцеплены: тред живёт по timestamp'у из своего
meta.json, песочница — по собственному mtime, парой они не удаляются.
Это согласуется с AR-2: артефакты живут до TTL для возможности
reuse_strategy=continue в будущем (после завершения процесса sandbox
помечен sandbox_status=completed, но физически жив).

Usage:
  python -m gc_server.swarm.cleanup \\
    --threads-dir ~/.local/state/gc/projects/<slug>/swarm/threads \\
    --sandbox-root-dir ~/.local/state/gc/projects/<slug>/swarm/sandboxes \\
    --ttl-hours 24 \\
    [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from typing import Any, Optional, Sequence


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _meta_timestamp(meta_path: str) -> Optional[datetime]:
    """Прочитать meta.json и вернуть самый поздний timestamp (updated_at >
    finished_at > created_at). None если не смогли распарсить или файл нечитаем."""
    try:
        with open(meta_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None

    candidates = []
    for top_key in ("updated_at", "created_at"):
        c = _parse_iso_datetime(data.get(top_key))
        if c is not None:
            candidates.append(c)

    sm = data.get("swarm_meta") or {}
    if isinstance(sm, dict):
        for key in ("finished_at", "started_at", "created_at"):
            c = _parse_iso_datetime(sm.get(key))
            if c is not None:
                candidates.append(c)

    if not candidates:
        return None
    return max(candidates)


def _remove_expired_dir(path: str, dry_run: bool, stats: dict) -> bool:
    if not dry_run:
        try:
            shutil.rmtree(path)
        except OSError:
            stats["errors"] += 1
            return False
    return True


def _cleanup_thread_dirs(
    threads_dir: str, cutoff: datetime, dry_run: bool, stats: dict
) -> None:
    for entry in sorted(os.listdir(threads_dir)):
        entry_path = os.path.join(threads_dir, entry)
        if not os.path.isdir(entry_path):
            continue

        meta_path = os.path.join(entry_path, "meta.json")
        ts = _meta_timestamp(meta_path) if os.path.isfile(meta_path) else None
        # Если meta.json нет/невалидный — fallback на mtime каталога.
        if ts is None:
            try:
                ts = datetime.fromtimestamp(
                    os.path.getmtime(entry_path),
                    tz=UTC,
                )
            except OSError:
                stats["errors"] += 1
                continue

        if ts >= cutoff:
            stats["kept"] += 1
            continue

        if not _remove_expired_dir(entry_path, dry_run, stats):
            continue
        stats["threads_removed"] += 1
        # Sandbox НЕ удаляем здесь — он чистится независимо, по своему mtime
        # (ниже), как обычная чистка тредов по возрасту. Расцеплено намеренно:
        # threads = запись о прогоне, sandbox = рабочий /work, у каждого свой TTL.


def _cleanup_sandbox_dirs(
    sandbox_root_dir: str, cutoff: datetime, dry_run: bool, stats: dict
) -> None:
    if os.path.isdir(sandbox_root_dir):
        for entry in sorted(os.listdir(sandbox_root_dir)):
            entry_path = os.path.join(sandbox_root_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            # Чистим ВСЕ песочницы старше TTL по их mtime (не только orphan'ы).
            try:
                mtime = datetime.fromtimestamp(
                    os.path.getmtime(entry_path),
                    tz=UTC,
                )
            except OSError:
                stats["errors"] += 1
                continue
            if mtime >= cutoff:
                continue  # свежая песочница — оставим
            if not _remove_expired_dir(entry_path, dry_run, stats):
                continue
            stats["sandboxes_removed"] += 1


def cleanup_threads(
    threads_dir: str,
    sandbox_root_dir: str,
    ttl_hours: float,
    *,
    dry_run: bool = False,
    extra_sandbox_roots: Sequence[str] = (),
) -> dict:
    """Удалить каталоги старше TTL. threads/<id> чистится по timestamp из meta.json,
    sandboxes/<id> — независимо, по своему mtime (расцеплено).

    ``extra_sandbox_roots`` — ещё корни песочниц тем же правилом: каталоги
    ``os_backend=sandbox, workdir=auto`` из gc_core (``<GC_STATE_DIR>/sandboxes``).

    Возвращает {threads_removed, sandboxes_removed, kept, errors}.
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=ttl_hours)

    stats = {
        "threads_removed": 0,
        "sandboxes_removed": 0,
        "kept": 0,
        "errors": 0,
    }

    # Sandboxes — независимо, по СОБСТВЕННОМУ mtime (а не по мете threads),
    # и до проверки threads_dir: песочницам ядра каталог тредов swarm не нужен.
    for root in (sandbox_root_dir, *extra_sandbox_roots):
        _cleanup_sandbox_dirs(root, cutoff, dry_run, stats)

    if os.path.isdir(threads_dir):
        _cleanup_thread_dirs(threads_dir, cutoff, dry_run, stats)

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--threads-dir", required=True, help="каталог swarm_threads/"
    )
    parser.add_argument(
        "--sandbox-root-dir", required=True, help=".swarm_sandboxes/"
    )
    parser.add_argument(
        "--ttl-hours",
        type=float,
        default=24.0,
        help="срок хранения в часах (default: 24)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только посчитать, ничего не удалять",
    )
    parser.add_argument(
        "--core-sandboxes-dir",
        action="append",
        default=[],
        help="<GC_STATE_DIR>/sandboxes/ (os_backend=sandbox, workdir=auto)",
    )
    args = parser.parse_args()

    stats = cleanup_threads(
        args.threads_dir,
        args.sandbox_root_dir,
        args.ttl_hours,
        dry_run=args.dry_run,
        extra_sandbox_roots=args.core_sandboxes_dir,
    )

    prefix = "[DRY RUN] " if args.dry_run else ""
    # Печатаем ровно те ключи, которые кладёт cleanup_threads(): threads_removed
    # / sandboxes_removed / kept / errors. Раньше здесь были ещё «matching» и
    # `stats['orphan_sandboxes_removed']` — оба из той версии, где песочница
    # удалялась ВМЕСТЕ со своим тредом, а «осиротевшие» досчитывались отдельным
    # проходом. Проходы давно слиты: песочницы чистятся независимо, по
    # собственному mtime (см. комментарий в cleanup_threads), и делить их на
    # «парные» и «сиротские» больше нечем — счётчик не восстанавливаем, а
    # убираем. Опечатка дожила до колеса, потому что этот файл был скопирован
    # байт-в-байт из scripts/cleanup_swarm_threads.py, а scripts/ не попадал ни
    # в одно колесо: KeyError вылетал ПОСЛЕ удаления каталогов, exit 1 при
    # выполненной работе, и cron ретраил.
    print(
        f"{prefix}swarm cleanup: removed {stats['threads_removed']} threads, "
        f"{stats['sandboxes_removed']} sandboxes; "
        f"kept {stats['kept']} active; "
        f"errors {stats['errors']}"
    )
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
