"""The MCP operation port: what an MCP call is, independent of where it runs.

`AgentRuntimeClient` speaks all six transports from the agent's own process —
this is the behaviour gc-cli has always had, moved behind an interface so a
second locus can exist beside it without either knowing about the other.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import httpx

from gc_core.tools.connect_mcp.abandonable import run_abandonable
from gc_core.tools.connect_mcp.client import (
    _acall_mcp_tool,
    _aget_or_init_session_id,
    _atools_list_request,
    forget_session,
)
from gc_core.tools.connect_mcp.durable_scope import require_no_reconnect
from gc_core.tools.connect_mcp.persistent_stdio import (
    PersistentStdioRegistry,
    is_persistent_stdio,
)
from gc_core.tools.connect_mcp.stream_client import (
    acall_tool_stream_http,
    acall_tool_stream_sse,
    acall_tool_stream_stdio,
    alist_tools_stream_http,
    alist_tools_stream_sse,
    alist_tools_stream_stdio,
)

_SUPPORTED_TRANSPORTS = (
    "http",
    "sse",
    "stdio",
    "stream_sse",
    "stream_http",
    "stream_stdio",
)


async def _mcp_call_under_stop(client: Any, tool_name: str, body) -> Any:
    """Снять текущий вызов. Процесс сервера сессии не трогаем."""
    import asyncio

    from gc_core.user_stop import (
        STOP_WAIT_ONLY,
        UserStopInterrupt,
        current_tool_call,
        current_user_stop,
    )

    stop = current_user_stop()
    if stop is None:
        return await body()
    if stop.requested:
        raise UserStopInterrupt(kind="mcp", started=False)
    _, call_id = current_tool_call()
    # MCP SDK enters anyio scopes here. Calls and teardown must stay in
    # the same task, including subsequent calls on a persistent session.
    task = asyncio.current_task()
    cancelled_by_stop = False

    async def _release():
        nonlocal cancelled_by_stop
        # Сессию не закрываем. Для долгоживущего stdio сигнал — уведомление
        # ``notifications/cancelled`` с id запроса; поздний ответ этого id
        # сессия сама не примет. ``False`` — запрос был, записать не удалось:
        # оборвано ожидание, не вызов. Прочие транспорты снимает отмена задачи
        # (SDK шлёт то же уведомление, streamable HTTP закрывает поток).
        outcome = None
        cancel = getattr(client, "cancel_inflight_request", None)
        if callable(cancel):
            outcome = cancel()
        if not task.done():
            cancelled_by_stop = task.cancel()
        if outcome is False:
            return STOP_WAIT_ONLY

    try:
        async with stop.arm(
            kind="mcp",
            tool=tool_name,
            call_id=call_id,
            mcp_server=str(getattr(client, "server_name", "") or ""),
            release=_release,
        ):
            return await body()
    except asyncio.CancelledError:
        if stop.requested:
            if cancelled_by_stop:
                task.uncancel()
            raise UserStopInterrupt(kind="mcp", started=True)
        raise


class McpClientPort(ABC):
    """Один MCP-сервер, две операции. Обе корутинные.

    Синхронных в контракте НЕТ. Они были, пока дискавери ходила двумя формами;
    после сведения её в одну (шаг A3) синхронный путь остался ровно у
    долгоживущего stdio — там обмен блокирующий по существу (``Popen``,
    сессия переживает разные ``asyncio.run``). Это деталь реализации
    конкретного клиента, а не обязательство порта: требовать вторую форму от
    всех значило бы заставлять каждого писать её ради одного случая.
    """

    def __init__(self, server_name: str, server_cfg: Dict[str, Any]) -> None:
        self.server_name = server_name
        self.server_cfg = server_cfg if isinstance(server_cfg, dict) else {}

    @abstractmethod
    async def alist_tools(self, *, timeout: float) -> List[Dict[str, Any]]:
        """Сырые записи ``tools/list`` — ровно как их вернул сервер.

        Порт приехал с develop синхронным, а дискавери и вызовы тулов здесь
        нативно асинхронны. Без этой пары async-путь звал бы синхронный метод и
        морозил loop на всё время сетевого обмена (у stdio — ещё и подпроцесса),
        то есть терял ровно то свойство, ради которого ветка и делалась.

        Синхронный вход остаётся, но НЕ как «второй способ сделать то же»:
        он живёт ровно для долгоживущего stdio, где обмен блокирующий по
        существу (``subprocess.Popen``: asyncio-подпроцесс привязан к
        создавшему его loop, а эту сессию заводит дискавери, зовут тулы и
        закрывает ``arun`` — три разных loop). Асинхронная форма сама уводит
        туда вызов через брошенный поток
        (``run_abandonable(self.list_tools, …)``), а всем прочим транспортам
        синхронный вход отказывает — см. ``AgentRuntimeClient.list_tools``.
        """

    @abstractmethod
    async def acall_tool(
        self, tool_name: str, arguments: Dict[str, Any], *, timeout: float
    ) -> Any:
        """Сырой ответ ``tools/call`` — ровно как его отдал транспорт.

        Не приводится к dict: легаси-транспорты отвечают списком или строкой,
        и вызывающий отдаёт такое значение модели как есть.
        """


def _effective_transport(server_cfg: Dict[str, Any]) -> str:
    """Транспорт, которым реально пойдёт запрос, — по ОБЩИМ правилам.

    Здесь была третья копия правил выбора, и она умела только половину:
    поднимала легаси-транспорт до SDK лишь при ``persistent_session``. Про
    депрекейт ``sse`` она не знала, поэтому непersistent ``transport: sse``
    уходил в одноразовый ``_arequest_sse`` — тот самый путь, про который
    докстрока политики говорит «405 на spec-серверах». Дискавери при этом шла
    через ``stream_sse``, и в лог писалось «Запрос отправлен через stream_sse»:
    сообщение было правдой про дискавери и ложью про вызов тула.

    Единственный источник правил — ``transport_policy.resolve_effective_transport``.
    """
    from gc_core.tools.connect_mcp.transport_policy import (
        resolve_effective_transport,
    )

    return resolve_effective_transport(server_cfg)


class AgentRuntimeClient(McpClientPort):
    """All six transports, spoken from the agent's own process."""

    def _require_transport(self) -> str:
        transport = str(self.server_cfg.get("transport") or "")
        if transport not in _SUPPORTED_TRANSPORTS:
            raise ValueError(f"unsupported transport: {transport!r}")
        return transport

    def _url(self) -> str:
        return str(self.server_cfg.get("url") or "")

    def _token(self) -> Optional[str]:
        token = self.server_cfg.get("token")
        return str(token) if token else None

    def list_tools(self, *, timeout: float) -> List[Dict[str, Any]]:
        """Только долгоживущий stdio. Остальные транспорты — асинхронной формой.

        Метод остался ради одного случая: обмен с долгоживущей сессией
        блокирующий (``Popen``, сессия переживает разные ``asyncio.run``), и
        асинхронный близнец сам уводит этот вызов сюда через брошенный поток.

        Легаси `http`/`sse` отсюда УШЛИ: до них доходила только синхронная
        дискавери, а форм дискавери больше не две. Транспорты SDK сюда не
        попадали и раньше.
        """
        self._require_transport()
        if not is_persistent_stdio(self.server_cfg):
            raise RuntimeError(
                f"MCP server '{self.server_name}': синхронная форма осталась "
                "только для persistent stdio; остальное — alist_tools/acall_tool"
            )
        session = self._bind_stdio_session()
        try:
            resp = session.call("tools/list", {}, timeout)
        finally:
            self._unbind_stdio_session(session)
        result = resp.get("result") if isinstance(resp, dict) else None
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return tools if isinstance(tools, list) else []

    def call_tool(
        self, tool_name: str, arguments: Dict[str, Any], *, timeout: float
    ) -> Any:
        """Только долгоживущий stdio — см. :meth:`list_tools`."""
        self._require_transport()
        if not is_persistent_stdio(self.server_cfg):
            raise RuntimeError(
                f"MCP server '{self.server_name}': синхронная форма осталась "
                "только для persistent stdio; остальное — alist_tools/acall_tool"
            )
        session = self._bind_stdio_session()
        try:
            return session.call(
                "tools/call",
                {"name": tool_name, "arguments": arguments},
                timeout,
            )
        finally:
            self._unbind_stdio_session(session)

    def cancel_inflight_request(self) -> "bool | None":
        """Снять текущий запрос долгоживущего stdio, если он уже отправлен.

        ``None`` — этой сессии снимать нечего (другой транспорт или запрос
        ещё не ушёл). Отмена задачи тогда сама доводит сигнал: SDK пишет
        ``notifications/cancelled``, streamable HTTP закрывает поток ответа.
        """
        session = getattr(self, "_stdio_session", None)
        if session is None:
            return None
        return session.cancel_inflight()

    def _bind_stdio_session(self):
        session = PersistentStdioRegistry.instance().get_or_create(
            self.server_name, self.server_cfg
        )
        self._stdio_session = session
        return session

    def _unbind_stdio_session(self, session: Any) -> None:
        if getattr(self, "_stdio_session", None) is session:
            self._stdio_session = None

    async def alist_tools(self, *, timeout: float) -> List[Dict[str, Any]]:
        transport = self._require_transport()
        effective = _effective_transport(self.server_cfg)
        url = self._url()
        token = self._token()

        if effective == "stream_sse":
            return await alist_tools_stream_sse(
                url=url,
                server_cfg=self.server_cfg,
                token=token,
                timeout=timeout,
            )
        if effective == "stream_http":
            return await alist_tools_stream_http(
                url=url,
                server_cfg=self.server_cfg,
                token=token,
                timeout=timeout,
            )
        if transport == "stream_stdio":
            return await alist_tools_stream_stdio(
                server_cfg=self.server_cfg, timeout=timeout
            )
        if is_persistent_stdio(self.server_cfg):
            # Долгоживущая сессия сидит на `subprocess.Popen` СОЗНАТЕЛЬНО:
            # asyncio-подпроцесс привязан к создавшему его loop, а эту сессию
            # заводит дискавери, зовут тулы и закрывает `arun` — три разных
            # loop. Прервать блокирующий обмен нечем, поэтому поток-демон.
            return await run_abandonable(self.list_tools, timeout=timeout)

        session_id = await _aget_or_init_session_id(
            self.server_name, url, self.server_cfg, token=token
        )
        if not session_id:
            raise RuntimeError(
                f"Failed to initialize MCP session for server '{self.server_name}'"
            )
        try:
            return await _atools_list_request(
                url,
                session_id=session_id,
                server_cfg=self.server_cfg,
                token=token,
            )
        except httpx.HTTPStatusError:
            forget_session(self.server_name)
            session_id = await _aget_or_init_session_id(
                self.server_name, url, self.server_cfg, token=token
            )
            if not session_id:
                raise RuntimeError(
                    f"Failed to re-initialize MCP session for server '{self.server_name}'"
                )
            return await _atools_list_request(
                url,
                session_id=session_id,
                server_cfg=self.server_cfg,
                token=token,
            )

    async def acall_tool(
        self, tool_name: str, arguments: Dict[str, Any], *, timeout: float
    ) -> Any:
        from gc_core.user_stop import current_user_stop

        if current_user_stop() is None:
            return await self._acall_tool_unstopped(
                tool_name, arguments, timeout=timeout
            )
        return await _mcp_call_under_stop(
            self,
            tool_name,
            lambda: self._acall_tool_unstopped(
                tool_name, arguments, timeout=timeout
            ),
        )

    async def _acall_tool_unstopped(
        self, tool_name: str, arguments: Dict[str, Any], *, timeout: float
    ) -> Any:
        transport = self._require_transport()
        effective = _effective_transport(self.server_cfg)
        url = self._url()
        token = self._token()

        if effective == "stream_sse":
            return await acall_tool_stream_sse(
                url=url,
                server_cfg=self.server_cfg,
                token=token,
                timeout=timeout,
                tool_name=tool_name,
                arguments=arguments,
            )
        if effective == "stream_http":
            return await acall_tool_stream_http(
                url=url,
                server_cfg=self.server_cfg,
                token=token,
                timeout=timeout,
                tool_name=tool_name,
                arguments=arguments,
            )
        if transport == "stream_stdio":
            return await acall_tool_stream_stdio(
                server_cfg=self.server_cfg,
                timeout=timeout,
                tool_name=tool_name,
                arguments=arguments,
            )
        if is_persistent_stdio(self.server_cfg):
            # См. `alist_tools`: сессия на `Popen` — сознательное ограничение.
            return await run_abandonable(
                self.call_tool, tool_name, arguments, timeout=timeout
            )

        session_id = await _aget_or_init_session_id(
            self.server_name,
            url,
            self.server_cfg,
            token=token,
            timeout=timeout,
        )
        if not session_id:
            raise RuntimeError(
                f"Failed to initialize MCP session for server '{self.server_name}'"
            )

        async def ado_call(sid: str) -> Any:
            return await _acall_mcp_tool(
                url=url,
                session_id=sid,
                tool_name=tool_name,
                arguments=arguments,
                server_cfg=self.server_cfg,
                token=token,
                timeout=timeout,
                server_key=self.server_name,
            )

        try:
            data = await ado_call(session_id)
        except httpx.HTTPStatusError:
            require_no_reconnect()
            forget_session(self.server_name)
            session_id = await _aget_or_init_session_id(
                self.server_name,
                url,
                self.server_cfg,
                token=token,
                timeout=timeout,
            )
            if not session_id:
                raise RuntimeError(
                    f"Failed to re-initialize MCP session for server '{self.server_name}'"
                )
            data = await ado_call(session_id)

        if data == {} and transport == "sse":
            require_no_reconnect()
            forget_session(self.server_name)
            session_id = await _aget_or_init_session_id(
                self.server_name,
                url,
                self.server_cfg,
                token=token,
                timeout=timeout,
            )
            if session_id:
                data = await ado_call(session_id)
        return data


__all__ = ["AgentRuntimeClient", "McpClientPort"]
