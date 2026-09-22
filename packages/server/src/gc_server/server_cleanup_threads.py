import logging
import threading
import time
from typing import Any, Dict

from fastapi import FastAPI
from gc_core.thread_store import resolve_threads_dir

_cleanup_thread_started = False
_swarm_cleanup_thread_started = False


# Чистки — обычные функции пакета, а не внешние скрипты. Раньше их искали
# файлом в scripts/ репозитория: путь считался от bundled_root(), который до
# разбиения на пакеты и был корнем репо, а после переезда ассетов стал вести в
# packages/core/assets/scripts/. Поток при этом исправно поднимался и раз в
# интервал звал несуществующий файл — subprocess.run шёл с check=False, код
# возврата никто не смотрел, исключения не было. Чистка тредов не работала
# вообще, молча. Вдобавок scripts/ не попадал НИ В ОДНО колесо, поэтому в
# установленном профиле её и не могло быть.


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
                logger.error("чистка тредов: %s ошибок, %s", stats["errors"], stats)
            elif stats.get("removed_meta"):
                # Удаление обязано оставлять след. Прежде молчал именно успешный
                # проход: «каталога нет» кричало предупреждением, а снос десятка
                # тредов не печатал ничего — громкость была устроена наоборот.
                logger.info(
                    "чистка тредов: удалено %d старше %d дн. из %s",
                    stats["removed_meta"], ttl_days, threads_dir,
                )
        except Exception as e:
            logger.warning("threads cleanup failed: %r", e)

        time.sleep(interval_seconds)


def register_cleanup_threads_on_startup(
    app: FastAPI,
    *,
    server_config: Dict[str, Any],
    base_agent_config: Dict[str, Any],
    logger: logging.Logger,
) -> None:
    """
    Подключает обработчик `startup`, который запускает поток чистки threads-файлов.

    Без lock-ов (предполагается 1 worker).
    """

    def _resolve_cleanup_cfg() -> tuple[int, int] | None:
        cleanup_cfg = server_config.get("threads_cleanup") or {}
        # Дефолт ВЫКЛЮЧЕН. Чистка удаляет треды из каталога, который принадлежит
        # не только серверу: `resolve_threads_dir` даёт то же самое, что у
        # `gc-cli` и TUI — проверено сравнением, а не по коду. То есть сервер,
        # поднятый в каталоге проекта, сносил бы историю интерактивной работы.
        # `docs/server.md` это прямо запрещает: «любые ротации/очистки
        # рекомендуется настраивать отдельно … вне этого приложения».
        #
        # Дефолт `True` пришёл с develop, но там у него не было этой цены: точки
        # входа `gc-serve` не существовало, а `uvicorn.run("src.server:app")`
        # резолвился только из корня репозитория — сервер из установленного
        # колеса не запускался в принципе. Ветка добавила `gc-serve`, и жестом
        # «поднял сервер в своём проекте» стало можно потерять переписку.
        #
        # Когда разрушающее действие затрагивает чужие данные, включать его
        # обязан человек. Цена ошибки в одну сторону — растущий диск и строка в
        # логе, в другую — безвозвратно удалённые треды.
        enabled = bool(cleanup_cfg.get("enabled", False))
        if not enabled:
            return None

        interval_seconds = cleanup_cfg.get("interval_seconds", 3600)
        ttl_days = cleanup_cfg.get("ttl_days", 7)
        try:
            interval_seconds = int(interval_seconds)
            ttl_days = int(ttl_days)
        except (TypeError, ValueError):
            interval_seconds = 3600
            ttl_days = 7
        return (interval_seconds, ttl_days)

    @app.on_event("startup")
    async def _startup_cleanup_threads() -> None:
        global _cleanup_thread_started

        if _cleanup_thread_started:
            return
        _cleanup_thread_started = True

        cfg = _resolve_cleanup_cfg()
        if cfg is None:
            return
        interval_seconds, ttl_days = cfg

        threads_dir = resolve_threads_dir(base_agent_config)
        # Объявляемся вслух. Строка стоит ЗДЕСЬ, а не на регистрации хука:
        # регистрация идёт на импорте модуля, до `logging.basicConfig` в
        # `serve()`, и запись ушла бы в никуда через `logging.lastResort`
        # (порог WARNING). Каталог общий с CLI и TUI, поэтому человек обязан
        # видеть, что именно чистится и с каким сроком.
        logger.info(
            "чистка тредов включена: каталог %s, ttl %d дн., интервал %d с",
            threads_dir, ttl_days, interval_seconds,
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


def register_swarm_cleanup_on_startup(
    app: FastAPI,
    *,
    swarm_config: Dict[str, Any],
    logger: logging.Logger,
) -> None:
    """Подключает шуруджи для TTL-очистки swarm-каталогов (шаг 3.2)."""

    if not swarm_config.get("enabled"):
        return

    ttl_seconds = swarm_config.get("ttl_seconds")
    try:
        ttl_seconds = int(ttl_seconds) if ttl_seconds is not None else 0
    except (TypeError, ValueError):
        ttl_seconds = 0
    if ttl_seconds <= 0:
        # TTL не задан → janitor отключен.
        return

    ttl_hours = ttl_seconds / 3600.0
    # Чистим раз в час максимум — TTL обычно сутки, чаще не нужно.
    interval_seconds = max(min(int(ttl_seconds / 10), 3600), 60)

    @app.on_event("startup")
    async def _startup_swarm_cleanup() -> None:
        global _swarm_cleanup_thread_started

        if _swarm_cleanup_thread_started:
            return
        _swarm_cleanup_thread_started = True

        swarm_threads_dir = str(swarm_config["threads_dir"])
        sandbox_root_dir = str(swarm_config["sandbox_root_dir"])

        threading.Thread(
            target=_swarm_cleanup_loop,
            kwargs={
                "interval_seconds": interval_seconds,
                "ttl_hours": ttl_hours,
                "swarm_threads_dir": swarm_threads_dir,
                "sandbox_root_dir": sandbox_root_dir,
                "logger": logger,
            },
            daemon=True,
        ).start()
        logger.info(
            "swarm janitor registered: interval=%ds ttl=%.1fh",
            interval_seconds, ttl_hours,
        )

