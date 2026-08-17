import asyncio
import threading
from typing import Any, Dict, Optional
from uuid import uuid4


def patch_asyncio_run_for_fastapi() -> None:
    """
    Безопасный monkey-patch asyncio.run для работы внутри FastAPI/uvicorn.

    - Если event loop не запущен — ведёт себя как обычный asyncio.run.
    - Если loop уже есть — выполняет корутину в отдельном треде с собственным loop.
    """
    _original_asyncio_run = asyncio.run

    def _safe_asyncio_run(coro, *args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _original_asyncio_run(coro, *args, **kwargs)

        result_box: Dict[str, Any] = {}
        error_box: Dict[str, BaseException] = {}

        def _runner() -> None:
            try:
                result_box["value"] = _original_asyncio_run(coro, *args, **kwargs)
            except BaseException as exc:  # noqa: BLE001
                error_box["error"] = exc

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()

        if "error" in error_box:
            raise error_box["error"]

        return result_box.get("value")

    asyncio.run = _safe_asyncio_run  # type: ignore[assignment]


def normalize_system(value: Optional[str], *, default_system: str) -> str:
    """
    Нормализовать идентификатор внешней системы:
    - обрезать пробелы, привести к нижнему регистру;
    - если пусто — взять default_system;
    - если всё ещё пусто — кинуть ValueError.
    """
    sys_norm = str(value or "").strip().lower()
    if not sys_norm:
        sys_norm = str(default_system or "").strip().lower()
    if not sys_norm:
        raise ValueError("system/source должен быть непустой строкой")
    return sys_norm


def ensure_task_id(payload: Dict[str, Any]) -> str:
    """
    Гарантировать наличие корректного task_id в данных.
    Если нет или некорректен — генерировать новый GUID и подставлять в payload.
    """
    raw = payload.get("task_id")
    task_id: Optional[str] = None
    if isinstance(raw, str) and raw.strip():
        task_id = raw.strip()
    if not task_id:
        task_id = uuid4().hex
        payload["task_id"] = task_id
    return task_id


__all__ = ["patch_asyncio_run_for_fastapi", "normalize_system", "ensure_task_id"]

