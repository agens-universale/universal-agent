"""
Жизненный цикл sworm-процесса: единая точка финализации.

Вызывается из:
  - swarm.monitor._run_loop (фоновая поэтапная проверка всех active sworm'ов);
  - server.py::get_swarm_status (lazy-чек при HTTP-запросе).

Логика финализации:

1. Если статус в meta.json уже финальный (completed/failed/timeout/unknown_failed)
   — ничего не делаем, возвращаем meta как есть.
2. Если completion.json существует (wrapper уже отчитался) — мерджим его поля
   в meta.json, определяем итоговый status по exit_code + error_type.
3. Если completion.json нет — проверяем PID через os.kill(pid, 0):
   - процесс жив → ничего не меняем (sworm ещё работает);
   - процесс мёртв (нет completion.json, нет процесса) → status=unknown_failed
     с error_type=process_crash.

Sandbox-каталог НЕ удаляется в финализации — он живёт до TTL janitor'а
(см. AR-2 в swarm_spec.md §10).
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time
from datetime import UTC, datetime
from typing import Any, Dict

from gc_server.swarm import storage as storage

logger = logging.getLogger(__name__)


FINAL_STATUSES = frozenset(
    {"completed", "failed", "timeout", "unknown_failed", "cancelled"}
)

# Grace-период между SIGTERM и SIGKILL для CLI-процесса (см. swarm_spec.md §6
# про summary compaction). Перекрывается с GNU timeout --kill-after=30 в wrapper.
_KILL_GRACE_SECONDS = 3.0

# Шаг 2.3+2.4 (AR-6): psutil — опциональная зависимость для memory/cpu monitoring.
try:
    import psutil  # type: ignore[import-not-found]

    _PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False


def _is_pid_alive(pid: int) -> bool:
    """Проверить, жив ли процесс с данным PID.

    Использует signal 0 (no-op), который проверяет существование без
    отправки реального сигнала. PermissionError означает что процесс
    существует, но мы не владелец (для нашего случая это «жив»).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_process_tree(
    pid: int, grace_period: float = _KILL_GRACE_SECONDS
) -> None:
    """Послать SIGTERM всей process group → подождать → SIGKILL если жив.

    Полагается на `start_new_session=True` в runner.py (шаг 1.6): PID == PGID,
    `os.killpg` убивает всё дерево (wrapper.sh + bwrap + CLI + дочерние CLI).

    Безопасно — если процесс уже мёртв или мы не владелец, выходим
    без ошибок (best effort).
    """
    if pid <= 0:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        # Группа уже не существует (или нет прав) — выход без ошибки.
        return

    # Wait для graceful exit (CLI может ещё дописывать summary).
    end_t = time.monotonic() + grace_period
    while time.monotonic() < end_t:
        if not _is_pid_alive(pid):
            return
        time.sleep(0.1)

    # Время вышло — добиваем.
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_iso_deadline(value: Any) -> datetime | None:
    """Распарсить ISO-timestamp deadline_at в aware datetime. None при ошибке."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    # Если timezone-naive (на всякий случай) — считаем UTC.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _get_dir_size_mb(dir_path: str) -> float | None:
    """Подсчитать размер директории в MB. None при ошибке.

    Используем `du -sb` если доступен (быстро на больших деревьях),
    иначе fallback на os.walk.
    """
    if not dir_path or not os.path.isdir(dir_path):
        return None
    # Сначала пробуем du -sb (GNU).
    try:
        result = subprocess.run(
            ["du", "-sb", dir_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            bytes_total = int(result.stdout.split()[0])
            return bytes_total / (1024 * 1024)
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
        pass
    # Fallback: os.walk.
    total = 0
    try:
        for root, _dirs, files in os.walk(dir_path):
            for fname in files:
                fp = os.path.join(root, fname)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    continue
    except OSError:
        return None
    return total / (1024 * 1024)


def _persist_agent_artifacts(
    threads_dir: str,
    swarm_id: str,
    sandbox_path: str,
    *,
    swarm_logs_dir: str = "",
) -> None:
    """Сохранить агентские ``.gc/logs`` из песочницы на хост.

    Назначение настраивается через ``swarm.swarm_logs_dir`` в config_server.yml:
      - задан абсолютный/относительный путь → ``<swarm_logs_dir>/<swarm_id>/``;
      - пусто → дефолт ``threads_dir/<swarm_id>/agent_logs/``.

    Вызывается при ЛЮБОЙ финализации (completion / timeout / resource-violation /
    crash), на хост-стороне. Поэтому логи агента сохраняются, даже если
    bwrap-песочница уже снята / убита по таймауту / процесс упал — файлы под
    ``/work`` физически лежат на диске хоста (bind), и мы их копируем независимо
    от тест-оркестратора.

    Best-effort: ошибки логируются, но финализацию не валят. Идемпотентно.
    """
    if not sandbox_path:
        return
    src = os.path.join(sandbox_path, ".gc", "logs")
    if not os.path.isdir(src):
        return
    if swarm_logs_dir:
        dst = os.path.join(swarm_logs_dir, swarm_id)
    else:
        dst = os.path.join(threads_dir, swarm_id, "agent_logs")
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)
    except OSError as e:
        logger.warning(
            "swarm %s: не удалось сохранить agent_logs из песочницы: %r",
            swarm_id,
            e,
        )


def _determine_status_from_completion(
    completion: Dict[str, Any],
) -> str:
    """Маппинг полей completion.json → финальный status.

    Wrapper.sh пишет error_type в одно из:
      "" (success), "timeout", "cli_error", "wrapper_setup_error",
      "signal_term", "disk_limit", "memory_limit", "cpu_limit".
    Сначала проверяем error_type, затем fallback на exit_code.
    """
    error_type = str(completion.get("error_type") or "").strip().lower()
    if error_type == "timeout":
        return "timeout"
    if error_type in {"cli_error", "wrapper_setup_error", "signal_term"}:
        return "failed"
    if error_type in {"disk_limit", "memory_limit", "cpu_limit"}:
        return "failed"

    exit_code = completion.get("exit_code")
    if isinstance(exit_code, int):
        return "completed" if exit_code == 0 else "failed"

    # Не смогли определить — консервативно failed.
    return "failed"


def _finalize_with_resource_violation(
    threads_dir: str,
    swarm_id: str,
    meta: Dict[str, Any],
    *,
    error_type: str,
    error_message: str,
    swarm_logs_dir: str = "",
) -> Dict[str, Any]:
    """Зафиксировать violation (disk/memory/cpu) в meta + status=failed."""
    patch = {
        "status": "failed",
        "finished_at": storage._utc_now_iso(),
        "error_type": error_type,
        "error_message": error_message,
        "sandbox_status": "completed",
    }
    base_meta = {
        "system": meta.get("system"),
        "task_id": meta.get("task_id"),
    }
    _persist_agent_artifacts(
        threads_dir,
        swarm_id,
        str((meta.get("swarm_meta") or {}).get("sandbox_path") or ""),
        swarm_logs_dir=swarm_logs_dir,
    )
    storage.save_swarm_meta(
        threads_dir,
        swarm_id,
        base_meta=base_meta,
        swarm_meta_patch=patch,
    )
    return storage.load_swarm_meta(threads_dir, swarm_id)


def _enforce_disk_limit(
    threads_dir: str,
    swarm_id: str,
    meta: Dict[str, Any],
    limits: dict,
    pid: Any,
    sandbox_path: str,
    swarm_logs_dir: str,
) -> Dict[str, Any] | None:
    # --- Disk quota (шаг 2.2) ---
    disk_limit_mb = limits.get("disk_mb")
    if (
        isinstance(disk_limit_mb, (int, float))
        and disk_limit_mb > 0
        and sandbox_path
    ):
        actual_mb = _get_dir_size_mb(sandbox_path)
        if actual_mb is not None and actual_mb > disk_limit_mb:
            if isinstance(pid, int) and _is_pid_alive(pid):
                _kill_process_tree(pid)
            logger.warning(
                "swarm %s killed by disk_limit (actual=%.1fMB, limit=%sMB)",
                swarm_id,
                actual_mb,
                disk_limit_mb,
            )
            return _finalize_with_resource_violation(
                threads_dir,
                swarm_id,
                meta,
                swarm_logs_dir=swarm_logs_dir,
                error_type="disk_limit",
                error_message=(
                    f"Sandbox disk usage ({actual_mb:.1f}MB) exceeds limit ({disk_limit_mb}MB)"
                ),
            )

    return None


def _enforce_memory_limit(
    threads_dir: str,
    swarm_id: str,
    meta: Dict[str, Any],
    limits: dict,
    pid: Any,
) -> Dict[str, Any] | None:
    # --- Memory limit через psutil (шаг 2.3) ---
    mem_limit_mb = limits.get("memory_mb")
    if (
        _PSUTIL_AVAILABLE
        and isinstance(mem_limit_mb, (int, float))
        and mem_limit_mb > 0
        and isinstance(pid, int)
        and _is_pid_alive(pid)
    ):
        try:
            proc = psutil.Process(pid)
            rss_mb = proc.memory_info().rss / (1024 * 1024)
            if rss_mb > mem_limit_mb:
                _kill_process_tree(pid)
                logger.warning(
                    "swarm %s killed by memory_limit (rss=%.1fMB, limit=%sMB)",
                    swarm_id,
                    rss_mb,
                    mem_limit_mb,
                )
                return _finalize_with_resource_violation(
                    threads_dir,
                    swarm_id,
                    meta,
                    error_type="memory_limit",
                    error_message=(
                        f"RSS ({rss_mb:.1f}MB) exceeds limit ({mem_limit_mb}MB)"
                    ),
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return None


def _enforce_cpu_limit(
    threads_dir: str,
    swarm_id: str,
    meta: Dict[str, Any],
    limits: dict,
    pid: Any,
) -> Dict[str, Any] | None:
    # --- CPU limit через psutil (шаг 2.4) ---
    cpu_limit = limits.get("cpu_percent")
    if (
        _PSUTIL_AVAILABLE
        and isinstance(cpu_limit, (int, float))
        and cpu_limit > 0
        and isinstance(pid, int)
        and _is_pid_alive(pid)
    ):
        try:
            proc = psutil.Process(pid)
            # interval=0.5 → блокировка на 500ms; нормально для monitor loop'а.
            cpu_pct = proc.cpu_percent(interval=0.5)
            if cpu_pct > cpu_limit:
                _kill_process_tree(pid)
                logger.warning(
                    "swarm %s killed by cpu_limit (cpu=%.0f%%, limit=%s%%)",
                    swarm_id,
                    cpu_pct,
                    cpu_limit,
                )
                return _finalize_with_resource_violation(
                    threads_dir,
                    swarm_id,
                    meta,
                    error_type="cpu_limit",
                    error_message=(
                        f"CPU ({cpu_pct:.0f}%) exceeds limit ({cpu_limit}%)"
                    ),
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return None


def _enforce_resource_limits(
    threads_dir: str,
    sandbox_root_dir: str,  # noqa: ARG001  # часть контракта: подпись менять нельзя
    swarm_id: str,
    meta: Dict[str, Any],
    swarm_meta: Dict[str, Any],
    *,
    swarm_logs_dir: str = "",
) -> Dict[str, Any] | None:
    """Проверить disk/memory/cpu лимиты. Если нарушены — kill + финализация.

    Вызывается из check_and_finalize только при heavy_checks=True (то есть из
    monitor loop'а, не из lazy-чека endpoint'а — там это было бы слишком дорого).

    Возвращает обновлённую meta если был violation; иначе None.
    """
    limits = swarm_meta.get("limits") or {}
    if not isinstance(limits, dict):
        return None

    pid = swarm_meta.get("pid")
    sandbox_path = swarm_meta.get("sandbox_path") or ""

    disk_violation = _enforce_disk_limit(
        threads_dir, swarm_id, meta, limits, pid, sandbox_path, swarm_logs_dir
    )
    if disk_violation is not None:
        return disk_violation
    memory_violation = _enforce_memory_limit(
        threads_dir, swarm_id, meta, limits, pid
    )
    if memory_violation is not None:
        return memory_violation
    return _enforce_cpu_limit(threads_dir, swarm_id, meta, limits, pid)


def _enforce_deadline(
    threads_dir: str,
    swarm_id: str,
    meta: Dict[str, Any],
    swarm_meta: Dict[str, Any],
    swarm_logs_dir: str,
) -> Dict[str, Any] | None:
    deadline = _parse_iso_deadline(swarm_meta.get("deadline_at"))
    if deadline is not None and _now_utc() > deadline:
        pid = swarm_meta.get("pid")
        if isinstance(pid, int) and _is_pid_alive(pid):
            _kill_process_tree(pid)
            logger.warning(
                "swarm %s killed by Python-side timeout (deadline_at=%s)",
                swarm_id,
                swarm_meta.get("deadline_at"),
            )
            patch: Dict[str, Any] = {
                "status": "timeout",
                "finished_at": storage._utc_now_iso(),
                "error_type": "timeout",
                "error_message": (
                    f"Killed by Python-side timeout after deadline {swarm_meta.get('deadline_at')}"
                ),
                "sandbox_status": "completed",
            }
            base_meta = {
                "system": meta.get("system"),
                "task_id": meta.get("task_id"),
            }
            _persist_agent_artifacts(
                threads_dir,
                swarm_id,
                str(swarm_meta.get("sandbox_path") or ""),
                swarm_logs_dir=swarm_logs_dir,
            )
            storage.save_swarm_meta(
                threads_dir,
                swarm_id,
                base_meta=base_meta,
                swarm_meta_patch=patch,
            )
            return storage.load_swarm_meta(threads_dir, swarm_id)
        # Иначе — process уже сам завершился, fallthrough к completion.json.

    return None


def check_and_finalize(
    threads_dir: str,
    sandbox_root_dir: str,
    swarm_id: str,
    *,
    heavy_checks: bool = False,
    swarm_logs_dir: str = "",
) -> Dict[str, Any]:
    """Проверить статус sworm'а и при необходимости финализировать meta.json.

    Args:
        threads_dir: каталог где хранится meta.json/completion.json/{stdout,stderr}.log.
        sandbox_root_dir: корень песочниц (для disk-quota check шага 2.2).
        swarm_id: идентификатор sworm'а вида "{system}_{task_id}".
        heavy_checks: если True — выполнить disk/memory/cpu enforcement
            (шаги 2.2-2.4). Используется monitor loop'ом, но НЕ lazy-чеком
            из server.py — там это было бы слишком дорого.

    Returns:
        Текущее (или обновлённое после финализации) содержимое meta.json.
        Пустой dict если meta вообще не существует.
    """
    meta = storage.load_swarm_meta(threads_dir, swarm_id)
    if not meta:
        return {}

    swarm_meta = meta.get("swarm_meta") or {}
    if not isinstance(swarm_meta, dict):
        swarm_meta = {}

    current_status = str(swarm_meta.get("status", "")).strip().lower()

    # 1. Уже финальный — возвращаем как есть.
    if current_status in FINAL_STATUSES:
        return meta

    # 2. Timeout enforcement (шаг 2.1).
    # Если deadline прошёл и процесс ещё жив — kill + status=timeout.
    # Это страховка поверх GNU timeout в wrapper'е (на минимальных
    # дистрибутивах timeout может отсутствовать).
    timeout_result = _enforce_deadline(
        threads_dir, swarm_id, meta, swarm_meta, swarm_logs_dir
    )
    if timeout_result is not None:
        return timeout_result

    # 2.5. Resource limits (только для heavy_checks=True, шаги 2.2-2.4).
    if heavy_checks:
        violation = _enforce_resource_limits(
            threads_dir,
            sandbox_root_dir,
            swarm_id,
            meta,
            swarm_meta,
            swarm_logs_dir=swarm_logs_dir,
        )
        if violation is not None:
            return violation

    # 3. completion.json есть — финализируем по нему.
    completion = storage.load_completion_info(threads_dir, swarm_id)
    if completion:
        new_status = _determine_status_from_completion(completion)
        patch: Dict[str, Any] = {
            "status": new_status,
            "exit_code": completion.get("exit_code"),
            "finished_at": completion.get("finished_at"),
            "error_type": completion.get("error_type", ""),
            "error_message": completion.get("error_message", ""),
            # Sandbox остаётся жить до TTL janitor'а (AR-2).
            "sandbox_status": "completed",
        }
        base_meta = {
            "system": meta.get("system"),
            "task_id": meta.get("task_id"),
        }
        _persist_agent_artifacts(
            threads_dir,
            swarm_id,
            str(swarm_meta.get("sandbox_path") or ""),
            swarm_logs_dir=swarm_logs_dir,
        )
        storage.save_swarm_meta(
            threads_dir,
            swarm_id,
            base_meta=base_meta,
            swarm_meta_patch=patch,
        )
        return storage.load_swarm_meta(threads_dir, swarm_id)

    # 4. completion.json нет — проверяем живость PID.
    pid = swarm_meta.get("pid")
    if isinstance(pid, int) and _is_pid_alive(pid):
        # Процесс жив, sworm работает — без изменений.
        return meta

    # PID мёртв и нет completion.json → процесс упал не отчитавшись.
    patch = {
        "status": "unknown_failed",
        "finished_at": storage._utc_now_iso(),
        "error_type": "process_crash",
        "error_message": "Process terminated without writing completion.json",
        "sandbox_status": "completed",
    }
    base_meta = {
        "system": meta.get("system"),
        "task_id": meta.get("task_id"),
    }
    _persist_agent_artifacts(
        threads_dir,
        swarm_id,
        str(swarm_meta.get("sandbox_path") or ""),
        swarm_logs_dir=swarm_logs_dir,
    )
    storage.save_swarm_meta(
        threads_dir,
        swarm_id,
        base_meta=base_meta,
        swarm_meta_patch=patch,
    )
    logger.warning(
        "swarm %s finalized as unknown_failed: PID=%s dead without completion.json",
        swarm_id,
        pid,
    )
    return storage.load_swarm_meta(threads_dir, swarm_id)


__all__ = [
    "FINAL_STATUSES",
    "_is_pid_alive",
    "_kill_process_tree",
    "check_and_finalize",
]
