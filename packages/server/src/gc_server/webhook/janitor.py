"""Фоновая чистка каталогов swarm (треды вебхука чистит сама поверхность —
через фасад тредов, ``WebhookSurface.aprune_once``).

Чистки — обычные функции пакета, а не внешние скрипты. Раньше их искали
файлом в scripts/ репозитория: путь считался от bundled_root(), который до
разбиения на пакеты и был корнем репо, а после переезда ассетов стал вести в
packages/core/assets/scripts/. Поток при этом исправно поднимался и раз в
интервал звал несуществующий файл — чистка тредов не работала вообще, молча.
"""

import logging
import threading
import time
from typing import Any, Dict


def _swarm_cleanup_loop(
    *,
    interval_seconds: int,
    ttl_hours: float,
    swarm_threads_dir: str,
    sandbox_root_dir: str,
    logger: logging.Logger,
) -> None:
    """Фоновая чистка swarm_threads/ + .swarm_sandboxes/ (M6, шаг 3.2)."""
    from gc_server.swarm.cleanup import cleanup_threads as cleanup_swarm

    while True:
        try:
            cleanup_swarm(swarm_threads_dir, sandbox_root_dir, ttl_hours)
        except Exception as e:
            logger.warning("swarm cleanup failed: %r", e)
        time.sleep(interval_seconds)


def start_swarm_cleanup(
    swarm_config: Dict[str, Any], logger: logging.Logger
) -> bool:
    """TTL-чистка swarm_threads/ и .swarm_sandboxes/; ``False`` — TTL не
    задан."""
    ttl_seconds = swarm_config.get("ttl_seconds")
    try:
        ttl_seconds = int(ttl_seconds) if ttl_seconds is not None else 0
    except (TypeError, ValueError):
        ttl_seconds = 0
    if ttl_seconds <= 0:
        return False
    ttl_hours = ttl_seconds / 3600.0
    # Чистим раз в час максимум — TTL обычно сутки, чаще не нужно.
    interval_seconds = max(min(int(ttl_seconds / 10), 3600), 60)
    threading.Thread(
        target=_swarm_cleanup_loop,
        kwargs={
            "interval_seconds": interval_seconds,
            "ttl_hours": ttl_hours,
            "swarm_threads_dir": str(swarm_config["threads_dir"]),
            "sandbox_root_dir": str(swarm_config["sandbox_root_dir"]),
            "logger": logger,
        },
        daemon=True,
    ).start()
    logger.info(
        "swarm janitor registered: interval=%ds ttl=%.1fh",
        interval_seconds,
        ttl_hours,
    )
    return True
