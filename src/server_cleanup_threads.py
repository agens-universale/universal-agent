import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict

from fastapi import FastAPI

from thread_store import resolve_threads_dir

_cleanup_thread_started = False


def _cleanup_threads_loop(
    *,
    interval_seconds: int,
    ttl_days: int,
    script_path: str,
    threads_dir: str,
    logger: logging.Logger,
) -> None:
    """
    Фоновая периодическая чистка устаревших тредов.

    Поток в рамках текущего процесса, чтобы не блокировать async event loop.
    """
    while True:
        try:
            subprocess.run(
                [
                    sys.executable,
                    script_path,
                    "--threads-dir",
                    threads_dir,
                    "--ttl-days",
                    str(ttl_days),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("threads cleanup failed: %r", e)

        time.sleep(interval_seconds)


def register_cleanup_threads_on_startup(
    app: FastAPI,
    *,
    server_config: Dict[str, Any],
    base_agent_config: Dict[str, Any],
    project_root: str,
    logger: logging.Logger,
) -> None:
    """
    Подключает обработчик `startup`, который запускает поток чистки threads-файлов.

    Без lock-ов (предполагается 1 worker).
    """

    def _resolve_cleanup_cfg() -> tuple[int, int] | None:
        cleanup_cfg = server_config.get("threads_cleanup") or {}
        enabled = bool(cleanup_cfg.get("enabled", True))
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
        global _cleanup_thread_started  # noqa: PLW0603

        if _cleanup_thread_started:
            return
        _cleanup_thread_started = True

        cfg = _resolve_cleanup_cfg()
        if cfg is None:
            return
        interval_seconds, ttl_days = cfg

        script_path = os.path.join(project_root, "scripts", "cleanup_threads_by_updated_at.py")
        threads_dir = resolve_threads_dir(base_agent_config)

        threading.Thread(
            target=_cleanup_threads_loop,
            kwargs={
                "interval_seconds": interval_seconds,
                "ttl_days": ttl_days,
                "script_path": script_path,
                "threads_dir": threads_dir,
                "logger": logger,
            },
            daemon=True,
        ).start()

