"""Фоновые чистки сервера: треды вебхука и каталоги swarm.

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


def _cleanup_threads_loop(
    *,
    interval_seconds: int,
    ttl_days: int,
    threads_dir: str,
    logger: logging.Logger,
) -> None:
    """Фоновая периодическая чистка устаревших тредов.

    Поток в рамках текущего процесса, чтобы не блокировать async event loop.
    """
    from gc_server.cleanup_threads import cleanup_threads

    while True:
        try:
            stats = cleanup_threads(threads_dir, ttl_days)
            if stats.get("missing_dir"):
                logger.warning("каталог тредов не найден: %s", threads_dir)
            elif stats["errors"]:
                logger.error(
                    "чистка тредов: %s ошибок, %s", stats["errors"], stats
                )
            elif stats.get("removed_meta"):
                # Удаление обязано оставлять след. Прежде молчал именно успешный
                # проход: «каталога нет» кричало предупреждением, а снос десятка
                # тредов не печатал ничего — громкость была устроена наоборот.
                logger.info(
                    "чистка тредов: удалено %d старше %d дн. из %s",
                    stats["removed_meta"],
                    ttl_days,
                    threads_dir,
                )
        except Exception as e:
            logger.warning("threads cleanup failed: %r", e)

        time.sleep(interval_seconds)


def start_threads_cleanup(
    *,
    enabled: bool,
    interval_seconds: int,
    ttl_days: float,
    threads_dir: str,
    logger: logging.Logger,
) -> bool:
    """Поток чистки тредов; ``False`` — выключена (умолчание).

    Дефолт ВЫКЛЮЧЕН: каталог тредов общий с ``gc-cli`` и TUI, и сервер,
    поднятый в каталоге проекта, сносил бы историю интерактивной работы.
    Когда разрушающее действие затрагивает чужие данные, включать его обязан
    человек.
    """
    if not enabled:
        return False
    # Каталог общий с CLI и TUI, поэтому человек обязан видеть, что именно
    # чистится и с каким сроком.
    logger.info(
        "чистка тредов включена: каталог %s, ttl %g дн., интервал %d с",
        threads_dir,
        ttl_days,
        interval_seconds,
    )
    threading.Thread(
        target=_cleanup_threads_loop,
        kwargs={
            "interval_seconds": interval_seconds,
            "ttl_days": ttl_days,
            "threads_dir": threads_dir,
            "logger": logger,
        },
        daemon=True,
    ).start()
    return True


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
