"""Attach a local ExecutionWorkspace to tools in unit tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping, Type, TypeVar, Union

from gc_core.config_modules.agent import (
    exec_output_capture_max_chars_from_config_dict,
)
from gc_core.execution_workspace import build_execution_workspace
from gc_core.settings import LoadedSettings, is_explicit

T = TypeVar("T")

#: Гарды, которые тест тулов получает выключенными, если не настроил их сам.
#:
#: Поведенческие тесты тулов (запись, чтение, exec, fetch) пишут во временные
#: каталоги вне ``launch_root`` и не отвечают на подтверждения. Прежде они
#: собирали контекст плоским словарём, и гард без своего ключа в нём считался
#: выключенным (runtime-fallback). Настоящие настройки secure-by-default —
#: здесь тот же прежний смысл задаётся явно. Тест, которому нужен гард,
#: настраивает его сам (``{"write_guard": {"enabled": True, ...}}``) — заданное
#: тестом не трогается.
_UNCONFIGURED_GUARDS: dict[str, Any] = {
    "security.write_guard.enabled": False,
    "security.write_guard.mode": "deny",
    "security.read_guard.enabled": False,
    "security.read_guard.mode": "deny",
    "security.url_policy.enabled": False,
    "security.url_policy.mode": "deny",
    "security.command_moderation.enabled": False,
    "security.command_moderation.mode": "allow",
}

ConfigLike = Union[Mapping[str, Any], LoadedSettings, None]


def tool_settings(config: ConfigLike = None) -> LoadedSettings:
    """``LoadedSettings`` для теста тула.

    ``config`` — описание теста (прежние плоские имена и/или пути через точку,
    как у :func:`gc_testkit.settings.make_settings`) либо готовые
    ``LoadedSettings`` (берутся как есть). Гарды, не заданные описанием,
    выключены — см. ``_UNCONFIGURED_GUARDS``.
    """
    from gc_testkit.settings import make_settings

    if isinstance(config, LoadedSettings):
        return config
    description = dict(config or {})
    loaded = make_settings(description)
    missing = {
        path: value
        for path, value in _UNCONFIGURED_GUARDS.items()
        if not is_explicit(loaded.settings, path)
    }
    if not missing:
        return loaded
    return make_settings({**description, **missing})


def local_ctx(config: ConfigLike = None, **attrs: Any) -> SimpleNamespace:
    """Minimal RunContext-like object with LocalFsBackend + workspace.

    Контекст несёт настоящие ``settings``/``runtime`` (см. :func:`tool_settings`);
    ``attrs`` — дополнительные атрибуты контекста (``logger=…``, ``state=…``).

    LocalFsBackend импортируется здесь, а не на уровне модуля: некоторые тесты
    вычищают ``tools.*`` из ``sys.modules`` и переимпортируют пакет — тогда
    module-level ссылка становится чужим классом относительно monkeypatch.
    """
    from gc_core.backends.local import LocalFsBackend

    from gc_testkit.settings import fake_ctx

    loaded = tool_settings(config)
    backend = LocalFsBackend(
        exec_output_capture_max_chars=exec_output_capture_max_chars_from_config_dict(
            loaded
        )
    )
    attrs.setdefault("os_backend", backend)
    attrs.setdefault(
        "workspace", build_execution_workspace(attrs["os_backend"], loaded)
    )
    return fake_ctx(loaded=loaded, **attrs)


class CallableTool:
    """Тест-адаптер: даёт синхронному тесту позвать асинхронный тул.

    У тулов форма ОДНА — ``acall``; синхронного ``__call__`` в проде нет и не
    должно быть. Но три сотни поведенческих тестов написаны как
    ``with_local_ctx(Tool)(path=..., content=...)``, и они проверяют ПОВЕДЕНИЕ
    тула, а не форму его вызова. Переписывать их на ``async def`` значит менять
    триста мест ради того, что тесту безразлично.

    Поэтому обёртка живёт здесь, в тест-ките, а не в проде: она гоняет ``acall``
    своим ``asyncio.run`` — тем же приёмом, что :func:`gc_testkit.run_coro`.
    Всё остальное (атрибуты, ``acall``, приватные поля) проксируется, поэтому
    тест, которому нужна именно асинхронная форма, зовёт ``.acall`` напрямую.

    Форму вызова сторожат отдельные гейты — ``test_file_tools_run_inline`` и
    ``test_native_async_tools``, — и они смотрят на настоящий тул, а не на эту
    обёртку.
    """

    __slots__ = ("_tool",)

    def __init__(self, tool: Any) -> None:
        object.__setattr__(self, "_tool", tool)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        import asyncio

        return asyncio.run(self._tool.acall(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_tool"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_tool"), name, value)

    def __repr__(self) -> str:
        return f"<callable {self._tool!r}>"


def with_local_ctx(
    tool_cls: Type[T], config: ConfigLike = None, **kwargs: Any
) -> T:
    """Instantiate ``tool_cls`` with a local ctx built from ``config``.

    Настройки тул читает из контекста; явный ``ctx=`` в ``kwargs`` побеждает.
    Возвращает тул, обёрнутый в :class:`CallableTool`: его можно и звать как
    раньше (обёртка сама прогонит ``acall``), и трогать как обычный объект.
    """
    kwargs.setdefault("ctx", local_ctx(config))
    return CallableTool(tool_cls(**kwargs))


__all__ = [
    "CallableTool",
    "local_ctx",
    "tool_settings",
    "with_local_ctx",
]
