"""Контроллер ACP: методы редактора → AsyncAgent и обратно."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any

from gc_core.thread_store import (
    ThreadSession,
    resolve_threads_dir,
    validate_thread_id,
)

from gc_acp.ask_human import bind_ask_human
from gc_acp.capabilities import initialize_result
from gc_acp.prompt import PromptRejected, flatten_prompt
from gc_acp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SESSION_NOT_FOUND,
    Incoming,
    error_message,
    notification,
    parse_line,
    result_message,
)
from gc_acp.session_index import ACP_SYSTEM, SessionFile, SessionIndex
from gc_acp.stop_reason import stop_reason
from gc_acp.tool_flow import ToolFlow
from gc_acp.updates import ChunkAssembler, history_updates

logger = logging.getLogger(__name__)

_SILENT = frozenset({"session/cancel", "$/cancel_request"})
_PARSE_ERROR = "Parse error"
_INVALID_REQUEST = "Invalid Request"
_METHOD_NOT_FOUND = "Method not found"
_INVALID_PARAMS = "Invalid params"
_INIT_REQUIRED = "initialize required"
_PROMPT_BUSY = "prompt already running"
_RUN_FAILED = "agent run failed"
_NO_SESSION = "Session not found"
_CWD_MISMATCH = "cwd mismatch"
_METHOD_NEW = "session/new"
_TOOL_POLL_S = 0.05


@dataclass
class LiveSession:
    session_id: str
    cwd: str
    agent: Any
    prompt_task: asyncio.Task | None = None
    prompt_id: Any = None
    cancel_requested: bool = False
    prompt_finished: bool = False
    response_sent: bool = False
    failed: bool = False


class AcpFacade:
    def __init__(
        self,
        *,
        config: dict,
        writer: Any,
        agent_factory: Any,
        auth_connector: Any = None,
        tools_cache: Any = None,
        os_backend: Any = None,
        session_index: SessionIndex | None = None,
    ) -> None:
        self._config = config
        self._writer = writer
        self._agent_factory = agent_factory
        self._auth = auth_connector
        self._tools_cache = tools_cache
        self._os_backend = os_backend
        self._index = session_index or SessionIndex(
            resolve_threads_dir(config)
        )
        self._sessions: dict[str, LiveSession] = {}
        self._initialized = False
        self._create_lock = asyncio.Lock()

    async def handle_line(self, line: str) -> None:
        incoming = parse_line(line)
        if incoming is None:
            return
        if incoming.parse_error:
            print("gc-acp: parse error", file=sys.stderr)
            if incoming.has_id:
                await self._send(
                    error_message(incoming.id, PARSE_ERROR, _PARSE_ERROR)
                )
            return
        try:
            await self._dispatch(incoming)
        except Exception:
            logger.exception("acp dispatch")
            if incoming.has_id and incoming.method not in _SILENT:
                await self._send(
                    error_message(incoming.id, INTERNAL_ERROR, _RUN_FAILED)
                )

    async def shutdown(self) -> None:
        for live in list(self._sessions.values()):
            await self._cancel_and_wait(live)
            await _safe_aclose(live.agent)
        self._sessions.clear()

    async def _dispatch(self, incoming: Incoming) -> None:
        if incoming.method in _SILENT:
            await self._on_notification(incoming)
            return
        if not incoming.has_id:
            if incoming.not_object or incoming.method is None:
                print("gc-acp: invalid request", file=sys.stderr)
            return
        if incoming.not_object or incoming.method is None:
            await self._send(
                error_message(incoming.id, INVALID_REQUEST, _INVALID_REQUEST)
            )
            return
        await self._on_request(incoming)

    async def _on_request(self, incoming: Incoming) -> None:
        if incoming.method != "initialize" and not self._initialized:
            await self._send(
                error_message(incoming.id, INVALID_REQUEST, _INIT_REQUIRED)
            )
            return
        handler = _request_handlers(self).get(incoming.method or "")
        if handler is None:
            await self._send(
                error_message(incoming.id, METHOD_NOT_FOUND, _METHOD_NOT_FOUND)
            )
            return
        await handler(incoming.id, incoming.params)

    async def _on_notification(self, incoming: Incoming) -> None:
        if incoming.method == "session/cancel":
            self._cancel_session(incoming.params)
            return
        if incoming.method == "$/cancel_request":
            self._cancel_by_request_id(incoming.params)

    async def _initialize(self, request_id: Any, _params: Any) -> None:
        self._initialized = True
        await self._send(result_message(request_id, initialize_result()))

    async def _session_new(self, request_id: Any, params: Any) -> None:
        body = _object_params(params)
        cwd = absolute_cwd(None if body is None else body.get("cwd"))
        servers = None if body is None else body.get("mcpServers")
        if body is None or cwd is None or not isinstance(servers, list):
            await self._invalid_params(request_id)
            return
        warn_ignored_client_fields(body)
        session_id = validate_thread_id(str(uuid.uuid4()))
        try:
            agent = await self._open_agent(cwd)
        except Exception:
            logger.exception(_METHOD_NEW)
            await self._fail(request_id)
            return
        try:
            self._index.create(session_id, cwd)
        except Exception:
            logger.exception(_METHOD_NEW)
            await _safe_aclose(agent)
            await self._fail(request_id)
            return
        self._sessions[session_id] = LiveSession(session_id, cwd, agent)
        await self._send(result_message(request_id, {"sessionId": session_id}))

    async def _session_prompt(self, request_id: Any, params: Any) -> None:
        body = _object_params(params)
        session_id = _session_id(
            None if body is None else body.get("sessionId")
        )
        live = self._sessions.get(session_id or "")
        if body is None or live is None:
            await self._invalid_params(request_id)
            return
        if _prompt_running(live):
            await self._send(
                error_message(request_id, INTERNAL_ERROR, _PROMPT_BUSY)
            )
            return
        try:
            text = flatten_prompt(body.get("prompt"))
        except PromptRejected:
            await self._invalid_params(request_id)
            return
        live.cancel_requested = False
        live.prompt_finished = False
        live.response_sent = False
        live.failed = False
        live.prompt_id = request_id
        live.prompt_task = asyncio.create_task(
            self._run_prompt(live, request_id, text)
        )

    async def _session_close(self, request_id: Any, params: Any) -> None:
        live = self._live_from(params)
        if live is None:
            await self._invalid_params(request_id)
            return
        await self._cancel_and_wait(live)
        await _safe_aclose(live.agent)
        self._sessions.pop(live.session_id, None)
        await self._send(result_message(request_id, {}))

    async def _session_list(self, request_id: Any, params: Any) -> None:
        body = _object_params(params)
        if body is None:
            await self._invalid_params(request_id)
            return
        cwd = body.get("cwd", None)
        if "cwd" in body and not isinstance(cwd, str):
            await self._invalid_params(request_id)
            return
        await self._send(
            result_message(
                request_id,
                {"sessions": self._index.list_sessions(cwd)},
            )
        )

    async def _session_resume(self, request_id: Any, params: Any) -> None:
        record = await self._disk_session(request_id, params, mcp=False)
        if record is None:
            return
        live = await self._ensure_live(record)
        if live is None:
            await self._fail(request_id)
            return
        await self._send(result_message(request_id, {}))

    async def _session_load(self, request_id: Any, params: Any) -> None:
        record = await self._disk_session(request_id, params, mcp=True)
        if record is None:
            return
        live = await self._ensure_live(record)
        if live is None:
            await self._fail(request_id)
            return
        try:
            await self._replay(record.session_id)
        except Exception:
            logger.exception("session/load")
            await self._fail(request_id)
            return
        await self._send(result_message(request_id, {}))

    async def _session_delete(self, request_id: Any, params: Any) -> None:
        session_id = _session_id_of(params)
        if session_id is None or self._index.load(session_id) is None:
            await self._missing(request_id)
            return
        live = self._sessions.pop(session_id, None)
        if live is not None:
            await self._cancel_and_wait(live)
            await _safe_aclose(live.agent)
        try:
            self._index.remove(session_id)
        except Exception:
            logger.exception("session/delete")
            await self._fail(request_id)
            return
        await self._send(result_message(request_id, {}))

    async def _disk_session(
        self, request_id: Any, params: Any, *, mcp: bool
    ) -> SessionFile | None:
        body = _object_params(params)
        session_id = _session_id(
            None if body is None else body.get("sessionId")
        )
        cwd = None if body is None else body.get("cwd")
        if (
            body is None
            or session_id is None
            or not isinstance(cwd, str)
            or cwd == ""
        ):
            await self._invalid_params(request_id)
            return None
        if mcp and not isinstance(body.get("mcpServers"), list):
            await self._invalid_params(request_id)
            return None
        record = self._index.load(session_id)
        if record is None:
            await self._missing(request_id)
            return None
        if os.path.normpath(cwd) != record.cwd:
            await self._send(
                error_message(request_id, SESSION_NOT_FOUND, _CWD_MISMATCH)
            )
            return None
        warn_ignored_client_fields(body)
        return record

    async def _ensure_live(self, record: SessionFile) -> LiveSession | None:
        live = self._sessions.get(record.session_id)
        if live is not None:
            return live
        try:
            agent = await self._open_agent(record.cwd)
        except Exception:
            logger.exception("acp agent")
            return None
        live = LiveSession(record.session_id, record.cwd, agent)
        self._sessions[record.session_id] = live
        return live

    async def _replay(self, session_id: str) -> None:
        thread = await ThreadSession.afrom_config(
            config_dict=self._config,
            system_id=ACP_SYSTEM,
            thread_id=session_id,
        )
        for update in history_updates(thread.get_full_history_messages()):
            await self._notify(session_id, update)

    async def _open_agent(self, cwd: str) -> Any:
        config = copy.deepcopy(self._config)
        config["launch_root"] = cwd
        async with self._create_lock:
            agent = await self._agent_factory(
                config,
                auth_connector=self._auth,
                tools_cache=self._tools_cache,
                os_backend=self._os_backend,
            )
        try:
            bind_ask_human(agent)
        except Exception:
            await _safe_aclose(agent)
            raise
        return agent

    async def _run_prompt(
        self, live: LiveSession, request_id: Any, text: str
    ) -> None:
        assembler = ChunkAssembler()
        try:
            await self._stream_prompt(live, assembler, text)
        except asyncio.CancelledError:
            live.cancel_requested = True
        except Exception:
            live.failed = True
            if not live.cancel_requested:
                logger.exception("session/prompt")
        if not live.response_sent:
            await self._close_prompt(live, request_id)
        _touch(self._index, live.session_id)

    async def _stream_prompt(
        self, live: LiveSession, assembler: ChunkAssembler, text: str
    ) -> None:
        flow = ToolFlow(self._index.thread_path(live.session_id))
        stop = asyncio.Event()
        watch = asyncio.create_task(self._watch_tools(live, flow, stop))
        try:
            await self._forward_deltas(live, assembler, text)
        finally:
            stop.set()
            await watch
            await self._emit_tools(live, flow)
        if live.cancel_requested:
            return
        final = getattr(live.agent, "last_answer", None)
        for update in assembler.flush_answer(final):
            await self._notify(live.session_id, update)

    async def _forward_deltas(
        self, live: LiveSession, assembler: ChunkAssembler, text: str
    ) -> None:
        async for kind, chunk in live.agent.stream(
            text, thread_id=live.session_id, system=ACP_SYSTEM
        ):
            for update in assembler.feed(kind, chunk):
                await self._notify(live.session_id, update)

    async def _watch_tools(
        self, live: LiveSession, flow: ToolFlow, stop: asyncio.Event
    ) -> None:
        """Пока тул исполняется, stream() не отдаёт дельт. Карточка — из файла."""
        while not stop.is_set():
            await self._emit_tools(live, flow)
            try:
                await asyncio.wait_for(stop.wait(), _TOOL_POLL_S)
            except TimeoutError:
                continue

    async def _emit_tools(self, live: LiveSession, flow: ToolFlow) -> None:
        for update in flow.poll():
            await self._notify(live.session_id, update)

    async def _close_prompt(self, live: LiveSession, request_id: Any) -> None:
        cancelled = live.cancel_requested
        live.prompt_finished = True
        live.response_sent = True
        if cancelled:
            await self._send(
                result_message(request_id, {"stopReason": "cancelled"})
            )
            return
        if live.failed:
            await self._send(
                error_message(request_id, INTERNAL_ERROR, _RUN_FAILED)
            )
            return
        reason = stop_reason(_outcome(live), cancelled=False)
        await self._send(result_message(request_id, {"stopReason": reason}))

    def _cancel_session(self, params: Any) -> None:
        live = self._live_from(params)
        if live is not None:
            _mark_cancel(live)

    def _cancel_by_request_id(self, params: Any) -> None:
        body = _object_params(params)
        if body is None or "id" not in body:
            return
        target = body.get("id")
        for live in self._sessions.values():
            if live.prompt_id == target:
                _mark_cancel(live)
                return

    def _live_from(self, params: Any) -> LiveSession | None:
        session_id = _session_id_of(params)
        if session_id is None:
            return None
        return self._sessions.get(session_id)

    async def _cancel_and_wait(self, live: LiveSession) -> None:
        _mark_cancel(live)
        task = live.prompt_task
        if task is not None and not task.done():
            await task

    async def _notify(self, session_id: str, update: dict) -> None:
        await self._send(
            notification(
                "session/update",
                {"sessionId": session_id, "update": update},
            )
        )

    async def _send(self, payload: dict) -> None:
        await self._writer.send(payload)

    async def _invalid_params(self, request_id: Any) -> None:
        await self._send(
            error_message(request_id, INVALID_PARAMS, _INVALID_PARAMS)
        )

    async def _missing(self, request_id: Any) -> None:
        await self._send(
            error_message(request_id, SESSION_NOT_FOUND, _NO_SESSION)
        )

    async def _fail(self, request_id: Any) -> None:
        await self._send(
            error_message(request_id, INTERNAL_ERROR, _RUN_FAILED)
        )


def _request_handlers(facade: AcpFacade) -> dict:
    return {
        "initialize": facade._initialize,
        _METHOD_NEW: facade._session_new,
        "session/prompt": facade._session_prompt,
        "session/close": facade._session_close,
        "session/list": facade._session_list,
        "session/resume": facade._session_resume,
        "session/load": facade._session_load,
        "session/delete": facade._session_delete,
    }


def absolute_cwd(value: Any) -> str | None:
    if not isinstance(value, str) or not os.path.isabs(value):
        return None
    return os.path.normpath(value)


def warn_ignored_client_fields(params: dict) -> None:
    servers = params.get("mcpServers")
    if isinstance(servers, list) and servers:
        print(
            "gc-acp: mcpServers из запроса не подключаются",
            file=sys.stderr,
        )
    extra = params.get("additionalDirectories")
    if isinstance(extra, list) and extra:
        print(
            "gc-acp: additionalDirectories не применяются",
            file=sys.stderr,
        )


def _object_params(params: Any) -> dict | None:
    if params is None:
        return {}
    if isinstance(params, dict):
        return params
    return None


def _session_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return validate_thread_id(value)
    except ValueError:
        return None


def _session_id_of(params: Any) -> str | None:
    body = _object_params(params)
    if body is None:
        return None
    return _session_id(body.get("sessionId"))


def _prompt_running(live: LiveSession) -> bool:
    task = live.prompt_task
    return task is not None and not task.done()


def _outcome(live: LiveSession) -> str | None:
    executor = getattr(live.agent, "executor", None)
    if executor is None:
        return None
    outcome = getattr(executor, "last_terminal_outcome", None)
    if outcome is None or isinstance(outcome, str):
        return outcome
    return None


def _mark_cancel(live: LiveSession) -> None:
    task = live.prompt_task
    if task is None or task.done() or live.prompt_finished:
        return
    live.cancel_requested = True
    stop = getattr(live.agent, "stop", None)
    if stop is not None:
        stop()


def _touch(index: SessionIndex, session_id: str) -> None:
    try:
        index.touch(session_id)
    except Exception:
        logger.exception("acp session touch")


async def _safe_aclose(agent: Any) -> None:
    close = getattr(agent, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        logger.exception("acp aclose")
