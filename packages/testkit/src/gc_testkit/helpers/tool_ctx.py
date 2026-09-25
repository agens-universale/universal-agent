"""Attach a local ExecutionWorkspace to tools in unit tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional, Type, TypeVar

from gc_core.config_modules.agent import (
    exec_output_capture_max_chars_from_config_dict,
)
from gc_core.execution_workspace import build_execution_workspace

T = TypeVar("T")


def local_ctx(config_dict: Optional[dict] = None) -> SimpleNamespace:
    """Minimal RunContext-like object with LocalFsBackend + workspace.

    LocalFsBackend импортируется здесь, а не на уровне модуля: некоторые тесты
    вычищают ``tools.*`` из ``sys.modules`` и переимпортируют пакет — тогда
    module-level ссылка становится чужим классом относительно monkeypatch.
    """
    from gc_core.backends.local import LocalFsBackend

    cfg = dict(config_dict or {})
    backend = LocalFsBackend(
        exec_output_capture_max_chars=exec_output_capture_max_chars_from_config_dict(
            cfg
        )
    )
    return SimpleNamespace(
        config_dict=cfg,
        os_backend=backend,
        workspace=build_execution_workspace(backend, cfg),
    )


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
    tool_cls: Type[T], *, config_dict: Optional[dict] = None, **kwargs: Any
) -> T:
    """Instantiate ``tool_cls`` with a local ctx (and optional config_dict).

    Возвращает тул, обёрнутый в :class:`CallableTool`: его можно и звать как
    раньше (обёртка сама прогонит ``acall``), и трогать как обычный объект.
    """
    cfg = config_dict if config_dict is not None else kwargs.get("config_dict")
    ctx_cfg = cfg if isinstance(cfg, dict) else {}
    kw = dict(kwargs)
    if cfg is not None:
        kw["config_dict"] = cfg
    kw.setdefault("ctx", local_ctx(ctx_cfg))
    return CallableTool(tool_cls(**kw))
