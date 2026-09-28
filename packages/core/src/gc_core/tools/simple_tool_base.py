from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from gc_core.execution_workspace import ExecutionWorkspace


class AbstractSimpleTool(ABC):
    def __init__(self, *args, **kwargs):
        self._ctx = kwargs.get("ctx")
        self._state = kwargs.get("state")
        self._config_dict = kwargs.get("config_dict")
        self._call_id = kwargs.get("call_id")

    @property
    def _backend(self):
        """Physical I/O port from ``RunContext.os_backend`` (set in ``create_run_context``)."""
        ctx = getattr(self, "_ctx", None)
        if ctx is None:
            raise RuntimeError(
                f"{type(self).__name__} requires ctx; use apply_tool "
                f"(tests: with_local_ctx / gc_testkit.harbor_fakes.bind_os_backend)"
            )
        backend = getattr(ctx, "os_backend", None)
        if backend is None:
            raise RuntimeError(
                f"{type(self).__name__}: ctx.os_backend is missing; use acreate_run_context()"
            )
        return backend

    @property
    def _workspace(self) -> ExecutionWorkspace:
        """Execution workspace from ``RunContext.workspace`` (assembled with the run)."""
        ctx = getattr(self, "_ctx", None)
        if ctx is None:
            raise RuntimeError(
                f"{type(self).__name__} requires ctx; use apply_tool "
                f"(tests: with_local_ctx / gc_testkit.harbor_fakes.bind_os_backend)"
            )
        workspace = getattr(ctx, "workspace", None)
        if not isinstance(workspace, ExecutionWorkspace):
            raise RuntimeError(
                f"{type(self).__name__}: ctx.workspace is missing; "
                f"acreate_run_context() must assemble ExecutionWorkspace"
            )
        return workspace

    @classmethod
    @abstractmethod
    def tool_description(
        cls, config_dict: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        pass

    @classmethod
    def registered_tool_name(cls) -> str:
        """API tool name from ``tool_description()`` (same as ``tools_data`` key)."""
        spec = cls.tool_description()
        if isinstance(spec, dict):
            name = spec.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return "unknown_tool"

    @classmethod
    @abstractmethod
    def prompt_description(
        cls, config_dict: Optional[Dict[str, Any]] = None
    ) -> str:
        pass

    @abstractmethod
    async def acall(self, *args, **kwargs) -> str:
        """Вызов тула. Форма ОДНА — асинхронная.

        Раньше абстрактным был синхронный ``__call__``, а ``acall`` считался
        необязательной «быстрой формой»: диспетчер смотрел, объявлена ли она, и
        без неё уводил тул в поток из общего пула. Это создавало две формы у
        каждого тула и третью ступень в диспетчере — ради возможности, которой
        нет: зарегистрировать свой Python-класс-тул извне нельзя вовсе,
        ``build_tools_data`` собирает набор из ``config_dict``, а точка
        расширения для чужих тулов — MCP, и она асинхронна насквозь.

        Асинхронность формы НЕ означает, что тело обязано что-то ждать.
        Файловые и короткие тулы исполняют его инлайн на loop, потому что у
        файлов нативной асинхронной формы нет, а поход в пул стоит дороже
        самой работы (40 мкс против микросекунд). Разница между «нативно» и
        «инлайн» живёт внутри ``acall`` и сторожится отдельно
        (``test_native_async_tools``, ``test_file_tools_run_inline``).
        """
        raise NotImplementedError

    @classmethod
    def prepare_args_before_strip(cls, args: Dict[str, Any]) -> Dict[str, Any]:
        """Adjust parsed args before injected schema fields are stripped.

        Override in tools that need to preserve injected values (e.g. remap
        injected ``reason`` into a native field). Default: no-op.
        """
        return args

    @classmethod
    def wrong_arguments_feedback_hint(
        cls,
        *,
        received_args: str,
        parsed_args: dict[str, Any] | None,
        _message: str,
    ) -> str | None:
        """
        Optional extra LLM hint when argument validation fails for this tool.

        Return None if there is nothing tool-specific to add.
        """
        return None
