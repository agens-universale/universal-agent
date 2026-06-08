#!/usr/bin/env python3
"""TTL-janitor для swarm-режима (шаг 3.1).

Удаляет каталоги swarm_threads/<id>/ старше TTL + соответствующие
sandbox-каталоги. Можно запускать вручную или из cron / startup-loop
(шаг 3.2).

Sandbox удаляется ВМЕСТЕ с meta — это согласуется с AR-2: артефакты
живут до TTL для возможности reuse_strategy=continue в будущем (после
завершения процесса sandbox помечен sandbox_status=completed, но
физически жив).

Usage:
  python scripts/cleanup_swarm_threads.py \\
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
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _meta_timestamp(meta_path: str) -> Optional[datetime]:
    """Прочитать meta.json и вернуть самый поздний timestamp (updated_at >
    finished_at > created_at). None если не смогли распарсить или файл нечитаем."""
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
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


def cleanup_threads(
    threads_dir: str,
    sandbox_root_dir: str,
    ttl_hours: float,
    *,
    dry_run: bool = False,
) -> dict:
    """Удалить каталоги старше TTL. Также удалить orphan sandbox-каталоги.

    Возвращает {threads_removed, sandboxes_removed, orphan_sandboxes_removed, kept}.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=ttl_hours)

    stats = {
        "threads_removed": 0,
        "sandboxes_removed": 0,
        "orphan_sandboxes_removed": 0,
        "kept": 0,
        "errors": 0,
    }

    if not os.path.isdir(threads_dir):
        return stats

    seen_ids = set()
    for entry in sorted(os.listdir(threads_dir)):
        entry_path = os.path.join(threads_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        seen_ids.add(entry)

        meta_path = os.path.join(entry_path, "meta.json")
        ts = _meta_timestamp(meta_path) if os.path.isfile(meta_path) else None
        # Если meta.json нет/невалидный — fallback на mtime каталога.
        if ts is None:
            try:
                ts = datetime.fromtimestamp(
                    os.path.getmtime(entry_path), tz=timezone.utc,
                )
            except OSError:
                stats["errors"] += 1
                continue

        if ts >= cutoff:
            stats["kept"] += 1
            continue

        # Удаляем threads/<id>/.
        if not dry_run:
            try:
                shutil.rmtree(entry_path)
            except OSError:
                stats["errors"] += 1
                continue
        stats["threads_removed"] += 1

        # И соответствующий sandbox.
        sandbox_path = os.path.join(sandbox_root_dir, entry)
        if os.path.isdir(sandbox_path):
            if not dry_run:
                try:
                    shutil.rmtree(sandbox_path)
                except OSError:
                    stats["errors"] += 1
                    continue
            stats["sandboxes_removed"] += 1

    # Orphan sandboxes (нет соответствующей threads/<id>/).
    if os.path.isdir(sandbox_root_dir):
        for entry in sorted(os.listdir(sandbox_root_dir)):
            entry_path = os.path.join(sandbox_root_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            if entry in seen_ids:
                continue  # уже обработан выше
            # Orphan — проверяем mtime.
            try:
                mtime = datetime.fromtimestamp(
                    os.path.getmtime(entry_path), tz=timezone.utc,
                )
            except OSError:
                stats["errors"] += 1
                continue
            if mtime >= cutoff:
                continue  # свежий orphan, оставим (вдруг скоро придёт meta)
            if not dry_run:
                try:
                    shutil.rmtree(entry_path)
                except OSError:
                    stats["errors"] += 1
                    continue
            stats["orphan_sandboxes_removed"] += 1

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--threads-dir", required=True, help="каталог swarm_threads/")
    parser.add_argument("--sandbox-root-dir", required=True, help=".swarm_sandboxes/")
    parser.add_argument(
        "--ttl-hours", type=float, default=24.0,
        help="срок хранения в часах (default: 24)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="только посчитать, ничего не удалять",
    )
    args = parser.parse_args()

    stats = cleanup_threads(
        args.threads_dir, args.sandbox_root_dir, args.ttl_hours,
        dry_run=args.dry_run,
    )

    prefix = "[DRY RUN] " if args.dry_run else ""
    print(
        f"{prefix}swarm cleanup: removed {stats['threads_removed']} threads, "
        f"{stats['sandboxes_removed']} matching sandboxes, "
        f"{stats['orphan_sandboxes_removed']} orphan sandboxes; "
        f"kept {stats['kept']} active; "
        f"errors {stats['errors']}"
    )
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
