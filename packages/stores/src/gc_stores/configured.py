"""Хранилища из ``storage:`` config.yml — для фронтендов (gc-serve, gc-cli).

Ядро знает только схему секции; открывает хранилища этот модуль::

    stores = await aopen_configured(settings)
    agent = await AsyncAgent.create(settings, stores=stores)
    ...
    await stores.aclose()

Тип части — ``files`` | ``postgres`` | ``mysql`` | ``sqlite`` | ``redis`` |
``s3`` или своя фабрика ``"пакет.модуль:функция"`` (получает параметры части
словарём). Одинаковые тип и подключение у ``threads`` и ``agent`` — одно
подключение на оба. Схема сверяется при открытии: не накатана или старая —
отказ с подсказкой ``gc-stores migrate`` (``ensure_schema: true`` — накатить).
"""

from __future__ import annotations

import importlib
from typing import Any, Callable

from gc_core.stores import Stores

FILES = "files"

#: Параметры части по типу: обязательный первым.
_PARAMS: dict[str, tuple[str, ...]] = {
    "postgres": ("dsn", "schema", "pool_options"),
    "mysql": ("dsn", "pool_options"),
    "sqlite": ("path",),
    "redis": ("url", "prefix"),
    "s3": ("bucket", "prefix", "endpoint_url", "region"),
}
#: Модуль драйвера → extra, который его ставит.
_DRIVERS = {
    "asyncpg": "postgres",
    "asyncmy": "mysql",
    "redis": "redis",
    "aiobotocore": "s3",
}


class StorageConfigError(ValueError):
    """``storage:`` не открывается: текст называет часть и что сделать."""


async def aopen_configured(settings: Any) -> Stores:
    """Открыть хранилища тредов и агента по ``storage:`` настроек."""
    from gc_core.settings import settings_of

    storage = settings_of(settings).storage
    threads_kind, threads_params = storage.part("threads")
    agent_kind, agent_params = storage.part("agent")
    agent_id = agent_params.pop("agent_id", None)
    try:
        stores = _open(
            settings,
            (threads_kind, threads_params),
            (agent_kind, agent_params),
            agent_id,
        )
    except ImportError as exc:
        raise _driver_error(exc) from None
    try:
        await _check_schema(stores, ensure=storage.ensure_schema)
    except ImportError as exc:
        await stores.aclose()
        raise _driver_error(exc) from None
    except BaseException:
        await stores.aclose()
        raise
    return stores


def open_part(settings: Any, part: str, *, agent_id: str | None = None) -> Any:
    """Одна часть ``storage:`` (``threads`` или ``agent``) — для утилиты
    ``gc-stores --config``. Файловая часть — отказ: схемы и загрузки у неё
    нет. ``agent_id`` перекрывает заданный в конфиге."""
    from gc_core.settings import settings_of

    kind, params = settings_of(settings).storage.part(part)
    if kind == FILES:
        raise StorageConfigError(
            f"storage.{part} — files: у файлового хранилища нет схемы и "
            f"загрузки в базу; задайте storage.{part}.type"
        )
    if part == "threads":
        return _open_threads(settings, kind, params)
    configured_id = params.pop("agent_id", None)
    return _open_agent(settings, kind, params, agent_id or configured_id)


def _open(settings, threads, agent, agent_id) -> Stores:
    (t_kind, t_params), (a_kind, a_params) = threads, agent
    if t_kind == a_kind and t_kind in _PARAMS and t_params == a_params:
        _check_params("threads", t_kind, t_params)
        return _open_pair(t_kind, t_params, _require_agent_id(agent_id))
    return Stores(
        threads=_open_threads(settings, t_kind, t_params),
        agent=_open_agent(settings, a_kind, a_params, agent_id),
    )


def _require_agent_id(agent_id: Any) -> str:
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise StorageConfigError(
            "storage.agent.agent_id обязателен для хранилища агента в базе: "
            "под ним лежат промпты, скиллы и память этого агента"
        )
    return agent_id


def _check_params(part: str, kind: str, params: dict[str, Any]) -> None:
    allowed = _PARAMS[kind]
    unknown = sorted(set(params) - set(allowed))
    if unknown:
        raise StorageConfigError(
            f"storage.{part}: у {kind} нет параметра {unknown[0]!r}; "
            f"допустимы {', '.join(allowed)}"
        )
    required = allowed[0]
    if not params.get(required):
        raise StorageConfigError(
            f"storage.{part}: для {kind} нужен {required}"
        )


def _open_pair(kind: str, params: dict[str, Any], agent_id: str) -> Stores:
    module = importlib.import_module(f"gc_stores.{kind}")
    first, *rest = _PARAMS[kind]
    options = {k: params[k] for k in rest if params.get(k) is not None}
    return module.open_stores(params[first], agent_id=agent_id, **options)


def _open_threads(settings, kind: str, params: dict[str, Any]) -> Any:
    if kind == FILES:
        _no_params("threads", kind, params)
        from gc_core.thread_paths import resolve_threads_dir
        from gc_core.threads.file_store import FileThreadStore

        return FileThreadStore(resolve_threads_dir(settings))
    if ":" in kind:
        return _factory(kind)(params)
    _known(kind, "threads")
    _check_params("threads", kind, params)
    module = importlib.import_module(f"gc_stores.{kind}")
    first, *rest = _PARAMS[kind]
    options = {k: params[k] for k in rest if params.get(k) is not None}
    store_class = _store_class(module, kind, "ThreadStore")
    return store_class(params[first], **options)


def _open_agent(settings, kind: str, params: dict[str, Any], agent_id) -> Any:
    if kind == FILES:
        _no_params("agent", kind, params)
        from gc_core.agent_store import FileAgentStore

        return FileAgentStore(settings)
    if ":" in kind:
        options = dict(params)
        if agent_id is not None:
            options["agent_id"] = agent_id
        return _factory(kind)(options)
    _known(kind, "agent")
    _check_params("agent", kind, params)
    agent_id = _require_agent_id(agent_id)
    module = importlib.import_module(f"gc_stores.{kind}")
    first, *rest = _PARAMS[kind]
    options = {k: params[k] for k in rest if params.get(k) is not None}
    store_class = _store_class(module, kind, "AgentStore")
    return store_class(params[first], agent_id=agent_id, **options)


def _store_class(module: Any, kind: str, suffix: str) -> Any:
    names = {
        "postgres": "Postgres",
        "mysql": "MySQL",
        "sqlite": "SQLite",
        "redis": "Redis",
        "s3": "S3",
    }
    return getattr(module, names[kind] + suffix)


def _known(kind: str, part: str) -> None:
    if kind not in _PARAMS:
        raise StorageConfigError(
            f"storage.{part}.type={kind!r} неизвестен: files, "
            f"{', '.join(sorted(_PARAMS))} или пакет.модуль:фабрика"
        )


def _no_params(part: str, kind: str, params: dict[str, Any]) -> None:
    if params:
        raise StorageConfigError(
            f"storage.{part}: у {kind} параметров нет, лишний "
            f"{sorted(params)[0]!r}"
        )


def _factory(path: str) -> Callable[[dict[str, Any]], Any]:
    module_name, _, attr = path.partition(":")
    try:
        module = importlib.import_module(module_name)
        return getattr(module, attr)
    except (ImportError, AttributeError) as exc:
        raise StorageConfigError(
            f"фабрика хранилища {path!r} не найдена: {exc}"
        ) from None


async def _check_schema(stores: Stores, *, ensure: bool) -> None:
    """Схема каждой базы (одна на две части — один раз): ``ensure`` —
    накатить недостающее, иначе сверить."""
    from gc_stores.migrations import SchemaMismatch

    seen: list[object] = []
    for store in (stores.threads, stores.agent):
        if any(store is s for s in seen):
            continue
        seen.append(store)
        method = getattr(
            store, "ensure_schema" if ensure else "check_schema", None
        )
        if method is None:
            continue
        try:
            await method()
        except SchemaMismatch as exc:
            raise StorageConfigError(
                f"{exc} — выполните gc-stores migrate --config <config.yml> "
                "или задайте storage.ensure_schema: true"
            ) from None


def _driver_error(exc: ImportError) -> StorageConfigError:
    """Нет драйвера базы — какой extra поставить (хранилища gc-stores уже
    называют его сами; голый ``ModuleNotFoundError`` — по имени модуля)."""
    name = (getattr(exc, "name", None) or "").split(".")[0]
    extra = _DRIVERS.get(name)
    if extra is None:
        return StorageConfigError(str(exc))
    return StorageConfigError(
        f"нет драйвера {name}: установите gc-stores[{extra}] "
        f"(или gc-server[{extra}])"
    )


__all__ = ["StorageConfigError", "aopen_configured", "open_part"]
