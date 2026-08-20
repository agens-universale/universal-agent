from typing import Any, Dict, Optional
from uuid import uuid4


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


__all__ = ["normalize_system", "ensure_task_id"]

