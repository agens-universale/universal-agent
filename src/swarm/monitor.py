import threading
import time
from typing import Dict, Optional, Set

import swarm.storage as storage


class SwarmMonitor:
    """
    Простое in-memory хранилище активных свормов и фоновой мониторинг их состояний.
    """

    def __init__(self, threads_dir: str, *, check_interval_seconds: int = 60) -> None:
        self._threads_dir = threads_dir
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

    # --- Фоновый монитор ---

    def start_in_background(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run_loop, name="swarm-monitor", daemon=True
        )
        self._thread.start()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(self._check_interval_seconds)
            # Снимаем лок только для копирования идентификаторов.
            with self._lock:
                ids_snapshot = list(self._active_ids)

            for swarm_id in ids_snapshot:
                meta = storage.load_swarm_meta(self._threads_dir, swarm_id)
                if not meta:
                    # Нечего мониторить, убираем.
                    self.unregister_active(swarm_id)
                    continue

                swarm_meta: Dict = meta.get("swarm_meta") or {}
                if not isinstance(swarm_meta, dict):
                    swarm_meta = {}
                status = str(swarm_meta.get("status", "")).strip().lower()

                # Финальные статусы — больше не считаем сворм активным.
                if status in {"completed", "failed", "timeout", "unknown_failed"}:
                    self.unregister_active(swarm_id)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)


__all__ = ["SwarmMonitor"]

