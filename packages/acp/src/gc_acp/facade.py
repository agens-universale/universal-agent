"""Контроллер ACP: методы редактора → AsyncAgent и обратно."""

from __future__ import annotations

import asyncio
import concurrent.futures
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
from gc_core.tools.turn.submit_solution import SubmitSolution

from gc_acp.ask_human import bind_ask_human
from gc_acp.capabilities import initialize_result
from gc_acp.confirm import (
    bind_confirm,
    broken_reply,
    cancelled_reply,
    choice_from_incoming,
    permission_message,
    timeout_sec,
)
from gc_acp.diffs import DiffBackend, DiffLog, attach_diff
from gc_acp.model_config import (
    config_options_payload,
    level_for_choice,
    listed,
    model_config_option,
    session_level,
)
from gc_acp.prompt import PromptRejected, flatten_prompt
from gc_acp.protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SESSION_NOT_FOUND,
    Incoming,
    PendingOutbound,
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

_METHOD_CANCEL_REQUEST = "$/cancel_request"
_SILENT = frozenset({"session/cancel", _METHOD_CANCEL_REQUEST})
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
_METHOD_DELETE = "session/delete"
_TOOL_POLL_S = 0.05
# Запас сверх confirm_timeout: обёртка с потока не должна сработать раньше
# внутреннего wait_for. Если всё же сработала после отправки запроса,
# закрытие пикера уходит отдельной задачей.
_CONFIRM_OUTER_SLACK_SEC = 1.0


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
    tool_flow: ToolFlow | None = None
    diffs: DiffLog | None = None
    # Выбор пикера. None — уровень конфига (``settings.level`` / ``--level``).
    # close забывает сессию вместе с этим полем; в файл конфига не пишется.
    level: str | None = None


class AcpFacade:
    def __init__(
        self,
        *,
        settings: Any,
        writer: Any,
        agent_factory: Any,
        auth_connector: Any = None,
        tools_cache: Any = None,
        os_backend: Any = None,
        session_index: SessionIndex | None = None,
    ) -> None:
        self._settings = settings
        self._writer = writer
        self._agent_factory = agent_factory
        self._auth = auth_connector
        self._tools_cache = tools_cache
        self._os_backend = os_backend
        self._index = session_index or SessionIndex(
            resolve_threads_dir(settings)
        )
        self._sessions: dict[str, LiveSession] = {}
        self._releasing: set[asyncio.Task] = set()
        self._initialized = False
        self._create_lock = asyncio.Lock()
        self._pending = PendingOutbound()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def handle_line(self, line: str) -> None:
        self._loop = asyncio.get_running_loop()
        incoming = parse_line(line)
        if incoming is None:
            return
        if incoming.parse_error:
            broken = broken_reply_for(incoming)
            if incoming.has_id and self._pending.resolve(broken):
                return
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

    def confirm_blocking(self, session_id: str, request: Any) -> str:
        """Синхронный confirm с потока тула. Таймаут и сбой — отказ без записи."""
        from gc_core.security.confirm_port import CONFIRM_TIMED_OUT

        loop = self._loop
        if loop is None or not loop.is_running():
            return CONFIRM_TIMED_OUT
        timeout = timeout_sec(self._settings)
        done, cancel = _schedule_confirm(
            loop, self._confirm(session_id, request)
        )
        try:
            return done.result(timeout=timeout + _CONFIRM_OUTER_SLACK_SEC)
        except Exception:
            loop.call_soon_threadsafe(cancel)
            return CONFIRM_TIMED_OUT

    async def shutdown(self) -> None:
        """Отменить живые ходы и закрыть простаивающих агентов.

        Идущий ход не ждём: stop() кооперативный, команда может занять
        весь таймаут, а stdin после выхода уже закрыт.
        """
        self._pending.cancel_all(cancelled_reply())
        leftover = list(self._sessions.values())
        self._sessions.clear()
        for live in leftover:
            if _prompt_running(live):
                _mark_cancel(live)
                continue
            await _safe_aclose(live.agent)

    async def _dispatch(self, incoming: Incoming) -> None:
        if incoming.method in _SILENT:
            await self._on_notification(incoming)
            return
        if (
            incoming.has_id
            and incoming.method is None
            and (incoming.has_result or incoming.has_error)
        ):
            # Ответ на наш request, не запрос клиента. Поздний selected
            # после таймаута или отмены решение не меняет и -32600 не шлём.
            self._pending.resolve(incoming)
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
            await self._cancel_session(incoming.params)
            return
        if incoming.method == _METHOD_CANCEL_REQUEST:
            await self._cancel_by_request_id(incoming.params)

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
        live = LiveSession(session_id, cwd, None)
        try:
            agent = await self._open_agent(live)
        except Exception:
            logger.exception(_METHOD_NEW)
            await self._fail(request_id)
            return
        live.agent = agent
        try:
            self._index.create(session_id, cwd)
        except Exception:
            logger.exception(_METHOD_NEW)
            await _safe_aclose(agent)
            await self._fail(request_id)
            return
        self._sessions[session_id] = live
        await self._send(
            result_message(
                request_id,
                {"sessionId": session_id, **self._model_fields(live)},
            )
        )

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
        live.tool_flow = None
        live.prompt_id = request_id
        live.prompt_task = asyncio.create_task(
            self._run_prompt(live, request_id, text)
        )

    async def _session_close(self, request_id: Any, params: Any) -> None:
        live = self._live_from(params)
        if live is None:
            await self._invalid_params(request_id)
            return
        self._sessions.pop(live.session_id, None)
        if _prompt_running(live):
            await self._release_later(live, drop_thread=False)
            await self._send(result_message(request_id, {}))
            return
        await self._release(live, drop_thread=False)
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
        await self._send(result_message(request_id, self._model_fields(live)))

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
        await self._send(result_message(request_id, self._model_fields(live)))

    async def _session_set_config_option(
        self, request_id: Any, params: Any
    ) -> None:
        body = _object_params(params)
        if body is None or _config_type_rejected(body):
            await self._invalid_params(request_id)
            return
        live = self._live_from(params)
        if (
            live is None
            or body.get("configId") != "model"
            or "value" not in body
        ):
            await self._invalid_params(request_id)
            return
        value = body.get("value")
        level = session_level(self._settings, live.level)
        option = model_config_option(self._settings, level)
        if not listed(option, value):
            await self._invalid_params(request_id)
            return
        chosen = level_for_choice(self._settings, level, value)
        if chosen is not None:
            live.level = chosen
            option = model_config_option(
                self._settings, session_level(self._settings, live.level)
            )
        await self._send(
            result_message(request_id, {"configOptions": [option]})
        )

    async def _session_delete(self, request_id: Any, params: Any) -> None:
        session_id = _session_id_of(params)
        if session_id is None or self._index.load(session_id) is None:
            await self._missing(request_id)
            return
        live = self._sessions.pop(session_id, None)
        if live is not None and _prompt_running(live):
            # Запись индекса снимаем сразу, чтобы load не поднял второй
            # агент на тот же тред. jsonl — после хода, он ещё пишется.
            try:
                self._index.drop_record(session_id)
            except Exception:
                logger.exception(_METHOD_DELETE)
                await self._fail(request_id)
                return
            await self._release_later(live, drop_thread=True)
            await self._send(result_message(request_id, {}))
            return
        if live is not None:
            await self._release(live, drop_thread=False)
        try:
            self._index.remove(session_id)
        except Exception:
            logger.exception(_METHOD_DELETE)
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
        live = LiveSession(record.session_id, record.cwd, None)
        try:
            agent = await self._open_agent(live)
        except Exception:
            logger.exception("acp agent")
            return None
        live.agent = agent
        self._sessions[record.session_id] = live
        return live

    async def _replay(self, session_id: str) -> None:
        thread = await ThreadSession.afrom_config(
            settings=self._settings,
            system_id=ACP_SYSTEM,
            thread_id=session_id,
        )
        for update in history_updates(
            thread.get_full_history_messages(),
            subagent_records=self._index.subagent_records(session_id),
        ):
            await self._notify(session_id, update)

    def _model_fields(self, live: LiveSession) -> dict:
        return config_options_payload(
            self._settings, session_level(self._settings, live.level)
        )

    async def _open_agent(self, live: LiveSession) -> Any:
        self._loop = asyncio.get_running_loop()
        settings = self._settings.with_runtime(launch_root=live.cwd)

        def level_provider() -> str:
            return session_level(self._settings, live.level)

        async with self._create_lock:
            agent = await self._agent_factory(
                settings,
                auth_connector=self._auth,
                tools_cache=self._tools_cache,
                os_backend=self._session_backend(live),
                level_provider=level_provider,
            )
        try:
            bind_ask_human(agent)
            bind_confirm(agent, live.session_id, self)
        except Exception:
            await _safe_aclose(agent)
            raise
        return agent

    def _session_backend(self, live: LiveSession) -> Any:
        """Обёртка диска этой сессии. Чужой бэкенд и журнал ядра не трогаем."""
        backend = self._os_backend
        if backend is None:
            return None
        live.diffs = DiffLog()
        return DiffBackend(
            backend,
            live.diffs,
            cwd=live.cwd,
            call_id=lambda: _open_call_id(live),
        )

    async def _confirm(self, session_id: str, request: Any) -> str:
        from gc_core.security.confirm_port import (
            CONFIRM_DENY_ONCE,
            CONFIRM_TIMED_OUT,
        )

        live = self._sessions.get(session_id)
        if live is None or live.cancel_requested:
            return CONFIRM_DENY_ONCE
        request_id = self._pending.next_id()
        future = asyncio.get_running_loop().create_future()
        self._pending.add(request_id, session_id, future)
        delivered = False
        try:
            flow = live.tool_flow
            call_id = None if flow is None else flow.current_call_id()
            # Пока send ждёт lock писателя, cancel уже мог снять future.
            # После этого await request не пишем: диалог в IDE нечем закрыть.
            if not await self._deliver_permission(
                session_id,
                future,
                permission_message(
                    request_id,
                    session_id,
                    request,
                    call_id,
                ),
            ):
                return CONFIRM_DENY_ONCE
            delivered = True
            try:
                incoming = await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=timeout_sec(self._settings),
                )
            except TimeoutError:
                # Ответ мог лечь в future в тот же тик, что и таймаут.
                if future.done() and not future.cancelled():
                    incoming = future.result()
                else:
                    await self._send(_cancel_notice(request_id))
                    return CONFIRM_TIMED_OUT
        except asyncio.CancelledError:
            # Обёртка с потока уже вернула timed_out. await здесь отменится
            # вместе с задачей, поэтому закрытие пикера — отдельная задача.
            if delivered:
                self._close_delivered_permission(request_id)
            raise
        finally:
            self._pending.discard(request_id)
        return choice_from_incoming(incoming)

    def _close_delivered_permission(self, request_id: Any) -> None:
        """Пикер уже у клиента. Отмена confirm не должна унести `$/cancel_request`."""
        try:
            task = asyncio.get_running_loop().create_task(
                self._send(_cancel_notice(request_id))
            )
        except RuntimeError:
            return
        self._releasing.add(task)
        task.add_done_callback(self._forget_release)

    async def _deliver_permission(
        self, session_id: str, future: asyncio.Future, payload: dict
    ) -> bool:
        """False — ожидание уже снято, ``session/request_permission`` не ушёл."""
        writer = self._writer
        lock = getattr(writer, "_lock", None)
        write_locked = getattr(writer, "write_locked", None)
        if isinstance(lock, asyncio.Lock) and callable(write_locked):
            await lock.acquire()
            try:
                if _permission_dropped(self._sessions, session_id, future):
                    return False
                write_locked(payload)
            finally:
                lock.release()
            return True
        await writer.send(payload)
        return not _permission_dropped(self._sessions, session_id, future)

    async def _run_prompt(
        self, live: LiveSession, request_id: Any, text: str
    ) -> None:
        assembler = ChunkAssembler()
        try:
            # Отмена после create_task и до старта хода пишет stop в state
            # прошлого прогона, а AsyncAgent.run его сбрасывает: ход шёл бы
            # целиком, клиенту всё равно ушёл бы cancelled. В stream не входим.
            if not live.cancel_requested:
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
        flow = ToolFlow(
            self._index.thread_path(live.session_id),
            sidecar_path=self._index.subagent_path(live.session_id),
        )
        live.tool_flow = flow
        stop = asyncio.Event()
        watch = asyncio.create_task(self._watch_tools(live, flow, stop))
        try:
            await self._forward_deltas(live, assembler, text)
        finally:
            stop.set()
            await watch
            await self._emit_tools(live, flow)
            live.tool_flow = None
        if live.cancel_requested:
            return
        final = SubmitSolution.present_result(
            getattr(live.agent, "last_answer", None),
            shown=assembler.answer,
        )
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
            await self._notify(
                live.session_id, attach_diff(update, live.diffs)
            )

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

    async def _cancel_session(self, params: Any) -> None:
        live = self._live_from(params)
        if live is None:
            return
        await self._dismiss_permissions(live.session_id)
        _mark_cancel(live)

    async def _cancel_by_request_id(self, params: Any) -> None:
        body = _object_params(params)
        if body is None or "requestId" not in body:
            return
        target = body.get("requestId")
        if self._pending.cancel_id(target, cancelled_reply()):
            return
        for live in self._sessions.values():
            if live.prompt_id == target:
                await self._dismiss_permissions(live.session_id)
                _mark_cancel(live)
                return

    async def _dismiss_permissions(self, session_id: str) -> None:
        """Снять ожидание и закрыть пикер: клиент узнаёт request по requestId."""
        for request_id in self._pending.cancel_session(
            session_id, cancelled_reply()
        ):
            await self._send(_cancel_notice(request_id))

    def _live_from(self, params: Any) -> LiveSession | None:
        session_id = _session_id_of(params)
        if session_id is None:
            return None
        return self._sessions.get(session_id)

    async def _release_later(
        self, live: LiveSession, *, drop_thread: bool
    ) -> None:
        """Ответ клиенту уже можно слать: ход досматривается отдельно."""
        await self._dismiss_permissions(live.session_id)
        _mark_cancel(live)
        task = asyncio.create_task(
            self._release(live, drop_thread=drop_thread)
        )
        self._releasing.add(task)
        task.add_done_callback(self._forget_release)

    def _forget_release(self, task: asyncio.Task) -> None:
        self._releasing.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("acp session release", exc_info=exc)

    async def _release(self, live: LiveSession, *, drop_thread: bool) -> None:
        task = live.prompt_task
        if task is not None and not task.done():
            await task
        await _safe_aclose(live.agent)
        if not drop_thread:
            return
        try:
            self._index.drop_thread(live.session_id)
        except Exception:
            logger.exception(_METHOD_DELETE)

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
        "session/set_config_option": facade._session_set_config_option,
        _METHOD_DELETE: facade._session_delete,
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


def _config_type_rejected(body: dict) -> bool:
    """``type`` для select не обязателен. ``boolean`` и прочие — отказ."""
    if "type" not in body:
        return False
    return body.get("type") != "select"


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


def _open_call_id(live: LiveSession) -> str | None:
    """Карточка, которая исполняется сейчас. До хода её ещё нет."""
    flow = live.tool_flow
    if flow is None:
        return None
    return flow.current_call_id()


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


def broken_reply_for(incoming: Incoming) -> Incoming:
    """Битый JSON с нашим id: закрыть ожидание, не подменяя чужой id."""
    reply = broken_reply()
    return Incoming(
        id=incoming.id,
        has_id=True,
        has_error=reply.has_error,
        error=reply.error,
        parse_error=True,
    )


def _schedule_confirm(
    loop: asyncio.AbstractEventLoop, coro: Any
) -> tuple[concurrent.futures.Future, Any]:
    """Поставить confirm на loop. ``cancel`` останавливает задачу, не Future."""
    done: concurrent.futures.Future = concurrent.futures.Future()
    state: dict[str, Any] = {"task": None, "drop": False}

    def deliver(task: asyncio.Task) -> None:
        if done.done():
            return
        if task.cancelled():
            done.cancel()
            return
        exc = task.exception()
        if exc is not None:
            done.set_exception(exc)
            return
        done.set_result(task.result())

    def schedule() -> None:
        if state["drop"]:
            coro.close()
            if not done.done():
                done.cancel()
            return
        task = loop.create_task(coro)
        state["task"] = task
        task.add_done_callback(deliver)

    def cancel() -> None:
        state["drop"] = True
        task = state["task"]
        if task is not None and not task.done():
            task.cancel()

    loop.call_soon_threadsafe(schedule)
    return done, cancel


def _cancel_notice(request_id: Any) -> dict:
    """Отмена своего request. В ACP v1 поле — ``requestId``, не ``id``."""
    return notification(_METHOD_CANCEL_REQUEST, {"requestId": request_id})


def _permission_dropped(
    sessions: dict[str, LiveSession], session_id: str, future: asyncio.Future
) -> bool:
    """Отмена или снятие сессии, пока отправка ждала lock."""
    if future.done():
        return True
    return session_id not in sessions


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
