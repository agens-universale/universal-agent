"""Связка хранилищ агента: треды и ресурсы — два независимых контракта.

Сочетания любые: треды в базе, ресурсы на диске, или всё в одной базе::

    Stores(threads=PostgresThreadStore(dsn), agent=FileAgentStore(settings))
    stores = gc_stores.postgres.open_stores(dsn, agent_id="coder")

Связку принимают ``AsyncAgent.create(settings, stores=…)`` и плагин
Temporal (``AgentPlugin(…, stores=…)``). Закрывает её владелец —
``await stores.aclose()``.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from gc_core.agent_store import AgentStore
from gc_core.threads.store import ThreadStore


@dataclass(frozen=True)
class Stores:
    """Хранилище тредов и хранилище агента одним объектом."""

    threads: ThreadStore
    agent: AgentStore

    async def aclose(self) -> None:
        """Закрыть оба (``close()``, если есть); один объект на обе роли —
        один раз. Общий пул двух разных объектов хранилища закрывают сами
        (повторное закрытие пула безвредно)."""
        closed: list[object] = []
        for store in (self.threads, self.agent):
            if any(store is seen for seen in closed):
                continue
            closed.append(store)
            close = getattr(store, "close", None)
            if inspect.iscoroutinefunction(close):
                await close()


class StorageNotOpenedError(ValueError):
    """В ``storage:`` задано не файловое хранилище, а этот запуск его не
    открыл: молча писать в файлы нельзя."""


def require_opened_storage(
    settings: object, *, thread_store: object, agent_store: object
) -> None:
    """Нефайловая часть ``storage:`` без переданного хранилища — отказ.

    Ядро хранилища по конфигу не открывает (реализации — в ``gc-stores``):
    это делает фронтенд и передаёт их сюда. Переданное явно конфиг не
    перекрывает.
    """
    from gc_core.config_modules.storage import FILES
    from gc_core.settings import settings_of

    storage = settings_of(settings).storage
    given = {"threads": thread_store, "agent": agent_store}
    for name, store in given.items():
        kind, _params = storage.part(name)
        if kind == FILES or store is not None:
            continue
        raise StorageNotOpenedError(
            f"storage.{name}.type={kind} в config.yml, но этот запуск "
            "хранилище не открыл: откройте его "
            "gc_stores.configured.aopen_configured(settings) и передайте "
            "stores= "
            "(gc-serve и gc-cli делают это сами; TUI и ACP хранят только "
            "в файлах)"
        )


def run_store_kwargs(settings: object, stores: Stores | None) -> dict:
    """``thread_store``/``agent_store`` для ``acreate_run_context`` — только
    нефайловые части ``storage:``: файловое хранилище прогон строит сам из
    своих настроек (у поверхностей сервера они свои)."""
    if stores is None:
        return {}
    from gc_core.config_modules.storage import FILES
    from gc_core.settings import settings_of

    storage = settings_of(settings).storage
    out: dict = {}
    if storage.part("threads")[0] != FILES:
        out["thread_store"] = stores.threads
    if storage.part("agent")[0] != FILES:
        out["agent_store"] = stores.agent
    return out


def files_only_rejection(settings: object, *, program: str) -> str | None:
    """Текст отказа фронтенда, который хранит только в файлах (TUI, ACP),
    если в ``storage:`` задана база; ``None`` — всё на файлах."""
    from gc_core.config_modules.storage import FILES, PARTS
    from gc_core.settings import settings_of

    storage = settings_of(settings).storage
    for name in PARTS:
        kind, _params = storage.part(name)
        if kind != FILES:
            return (
                f"{program}: storage.{name}.type={kind} в config.yml, а "
                f"{program} хранит треды и ресурсы агента только в файлах; "
                "хранилище в базе открывают gc-serve и gc-cli — уберите "
                "секцию storage или запустите другой конфиг (--config)"
            )
    return None


__all__ = [
    "StorageNotOpenedError",
    "Stores",
    "files_only_rejection",
    "require_opened_storage",
    "run_store_kwargs",
]
