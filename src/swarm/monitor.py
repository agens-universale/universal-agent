"""In-memory мониторинг активных sworm'ов.

Шаги M1/1.4 (background loop вызывает check_and_finalize) и
M2/1.5 (lazy-чек интегрирован в server.py).

Loop работает асинхронно от HTTP-запросов: даже если никто не дёргает
/swarm/status, sworm'ы со временем доходят до финального статуса
автоматически.
"""

import logging
import threading
import time
from typing import Dict, Optional, Set

import swarm.lifecycle as lifecycle
import swarm.storage as storage


logger = logging.getLogger(__name__)


class SwarmMonitor:
    """
    Простое in-memory хранилище активных свормов и фоновой мониторинг их состояний.
    """

    def __init__(
        self,
        threads_dir: str,
        sandbox_root_dir: str = "",
        *,
        check_interval_seconds: int = 60,
        swarm_logs_dir: str = "",
    ) -> None:
        self._threads_dir = threads_dir
        self._sandbox_root_dir = sandbox_root_dir
        self._swarm_logs_dir = swarm_logs_dir
        self._check_interval_seconds = check_interval_seconds
        self._active_ids: Set[str] = set()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # --- API для сервера ---

    def register_active(self, swarm_id: str) -> None:
        with self._lock:
            self._active_ids.add(swarm_id)

    def unregister_active(self, swarm_id: str) -> None:
        with self._lock:
            self._active_ids.discard(swarm_id)

    def get_active_count(self) -> int:
        with self._lock:
            return len(self._active_ids)

    def get_active_ids_copy(self) -> Set[str]:
        """Снимок active set'а — для graceful shutdown / диагностики."""
        with self._lock:
            return set(self._active_ids)

    # --- Фоновый монитор ---

    def start_in_background(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run_loop, name="swarm-monitor", daemon=True
        )
        self._thread.start()

    def _check_one_swarm(self, swarm_id: str) -> None:
        """Проверить один sworm и снять с регистрации если он финализирован.

        Делегирует финализацию `lifecycle.check_and_finalize` — единая точка
        входа для monitor loop'а и lazy-чека в server.py.

        Monitor loop вызывает с heavy_checks=True — это включает enforcement
        ресурсных лимитов (disk/memory/cpu, шаги 2.2-2.4). Lazy-чек в
        server.py использует дефолт False, чтобы не блокировать HTTP-запрос
        на дорогих операциях вроде psutil.cpu_percent(interval=0.5).
        """
        try:
            meta = lifecycle.check_and_finalize(
                self._threads_dir,
                self._sandbox_root_dir,
                swarm_id,
                heavy_checks=True,
                swarm_logs_dir=self._swarm_logs_dir,
            )
        except Exception:
            logger.exception("swarm monitor: check_and_finalize failed for %s", swarm_id)
            return

        if not meta:
            # meta удалили (janitor / ручная очистка) — больше нечего мониторить.
            self.unregister_active(swarm_id)
            return

        swarm_meta = meta.get("swarm_meta") or {}
        if not isinstance(swarm_meta, dict):
            swarm_meta = {}
        status = str(swarm_meta.get("status", "")).strip().lower()

        if status in lifecycle.FINAL_STATUSES:
            self.unregister_active(swarm_id)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            # Sleep с прерыванием по stop_event (вместо time.sleep), чтобы
            # graceful shutdown не ждал полные check_interval_seconds.
            if self._stop_event.wait(self._check_interval_seconds):
                break

            with self._lock:
                ids_snapshot = list(self._active_ids)

            for swarm_id in ids_snapshot:
                self._check_one_swarm(swarm_id)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)


__all__ = ["SwarmMonitor"]
