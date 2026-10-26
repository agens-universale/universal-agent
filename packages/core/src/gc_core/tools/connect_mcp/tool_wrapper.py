"""MCP tool identity; transport dependencies are loaded only by its leaf body."""

from typing import Any, Dict


class MCPToolWrapper:
    """
    Обёртка над MCP-инструментом с интерфейсом как у локальных тулов:
    __call__(**arguments) -> str | dict | list.
    """

    def __init__(
        self,
        full_tool_name: str,
        servers_cfg: Dict[str, Dict[str, Any]] | None,
        tools_registry: Dict[str, Dict[str, Any]] | None,
        timeout: float,
        default_use_retry: bool,
        *,
        os_backend: Any = None,
        caller: Any = None,
    ) -> None:
        self._full_tool_name = full_tool_name
        self._servers_cfg = servers_cfg
        self._tools_registry = tools_registry
        self._timeout = timeout
        self._default_use_retry = default_use_retry
        self._os_backend = os_backend
        self._caller = caller

    async def acall(self, **arguments: Any) -> Any:
        """Быстрый путь для цикла агента: диспетчер зовёт его без потока.

        Форма из docs/async.md — тул с парой ``acall``/``__call__``.
        Для SDK-транспортов вызов идёт прямо на loop вызывающего; легаси
        (свои http/stdio на requests и subprocess) уходят в поток внутри
        ``acall_mcp_tool``, потому что прервать их всё равно нечем.
        """
        if self._servers_cfg is None or self._tools_registry is None:
            from gc_core.execution.contracts import ExecutionFailure

            raise ExecutionFailure(
                "Prepared MCP tools require a leaf effect executor"
            )
        from gc_core.tools.connect_mcp.agent_tools import acall_mcp_tool

        caller = self._caller or acall_mcp_tool
        return await caller(
            full_tool_name=self._full_tool_name,
            arguments=arguments,
            servers_cfg=self._servers_cfg,
            tools_registry=self._tools_registry,
            timeout=self._timeout,
            default_use_retry=self._default_use_retry,
            os_backend=self._os_backend,
        )
