from typing import Any, Dict, Optional
from uuid import uuid4

from gc_core.thread_store import validate_thread_id


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
    Если не задан — генерировать новый GUID и подставлять в payload.
    Если задан, но негоден — ``ValueError``; вызывающий обязан ответить 400.

    Присланный `task_id` уезжает в ИМЯ КАТАЛОГА: `swarm_<system>_<task_id>`, и
    дальше в `makedirs`, в запись `meta.json`, в `--bind … /work` внутрь
    песочницы (то есть монтированием на запись) и в `shutil.rmtree` при провале
    запуска. Один `strip()` от `../../` не защищает: `normpath` схлопывает путь
    наружу, а проверка `not os.path.isdir(sandbox_dir)` промахивается, потому что
    литерального сегмента `swarm_generic_..` на диске нет.

    Валидируем ЗДЕСЬ, а не в `_make_swarm_id`: эндпоинты `/swarm/status` и
    `/swarm/logs` собирают идентификатор инлайном, мимо него, и через них
    читается произвольный файл обычным GET.

    Вебхуку это ничего не ломает: его `task_id` и так обязан пройти
    `validate_thread_id` ниже по потоку, в `thread_store`, — просто падал позже
    и грязнее, уже после ответа `200 started`. Живые формы (`PROJ-123`,
    `build-42`, `2026-09-01_run-7`) проходят; отвергаются те, что и раньше были
    сломаны контрактом стора.
    """
    raw = payload.get("task_id")
    if isinstance(raw, str) and raw.strip():
        return validate_thread_id(raw)
    task_id = uuid4().hex
    payload["task_id"] = task_id
    return task_id


__all__ = ["normalize_system", "ensure_task_id"]

