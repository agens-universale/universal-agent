"""One MCP operation = one stateless exchange inside the tool-layer environment.

Nothing is installed in the image, no file is written, no process is left
running. Each agent-level operation (`tools/list`, `tools/call`) carries its own
`initialize` -> `notifications/initialized` -> method handshake and closes the
server-side session with a DELETE when it is done; the sim-user sidecars this
exists for keep their state in the server process, not in the MCP session.

The script that runs in the container is stdlib-only and does the minimum
framing work needed to know when to stop reading: an SSE answer must not be read
to EOF, because per MCP Streamable HTTP the server only SHOULD close the stream
after the final result.

`python3` is the only carrier. `curl`/`wget` cannot report the response status or
headers (so `mcp-session-id` would be lost), cannot stop at the first SSE frame,
and would need a scratch file — a carrier that is wrong in three ways is worse
than a clear error, and every image this targets ships python3.
"""

import base64
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from gc_core.backends.async_protocol import aexec_port, arun_backend_exec_raw
from gc_core.backends.protocol import WALK_OUTPUT_MAX_CHARS
from gc_core.tools.connect_mcp.client import (
    MCP_INIT_PARAMS,
    _merge_mcp_headers,
    _normalize_url,
)
from gc_core.tools.connect_mcp.client_port import McpClientPort
from gc_core.utils.ssl_config import get_verify_ssl

logger = logging.getLogger(__name__)

EXEC_RESULT_MARKER = "__GC_MCP_EXEC__"

# One operation is three requests (initialize, initialized, method). The caller's
# timeout is the budget for the whole operation: each request's exec deadline is
# one share of it, so the three cannot add up past the deadline that granted them
# (the one-off carrier probe per backend is the only thing outside this sum).
_REQUESTS_PER_OPERATION = 3

# Inside a share, the in-script socket timeout ends before the exec kill, so a
# slow answer surfaces as an MCP timeout rather than an opaque exec one. The
# margin shrinks on small budgets — its job is ordering, not size.
_TIMEOUT_SLACK_SEC = 15
_TIMEOUT_SLACK_SHARE = 0.2

# The python3 probe runs once per backend; it does not need a full share.
_PROBE_TIMEOUT_SEC = 10


def _split_share(share: float) -> tuple[float, int]:
    """(socket_timeout, exec_deadline) for one request's share of the budget.

    Both come off the integer deadline. Taking the margin off the fractional
    share instead lets truncation eat it — a 2.67s share left a 2.13s socket
    against a 2s deadline — which inverts the ordering the margin exists for.
    """
    deadline = max(1, int(share))
    slack = min(_TIMEOUT_SLACK_SEC, deadline * _TIMEOUT_SLACK_SHARE)
    socket_timeout = max(1.0, deadline - slack)
    return socket_timeout, deadline


# Attribute the python3 probe is cached under, on the backend rather than on the
# client: carrier availability is a property of the environment, and clients are
# rebuilt per operation.
_CARRIER_CACHE_ATTR = "_gc_mcp_has_python3"


def _tools_from_body(body: Any) -> List[Dict[str, Any]]:
    """Тело tools/list → список тулов. Общее для синхронной и асинхронной форм."""
    result = body.get("result") if isinstance(body, dict) else None
    tools = result.get("tools", []) if isinstance(result, dict) else []
    return tools if isinstance(tools, list) else []


class McpExecCarrierError(RuntimeError):
    """The exec carrier could not deliver a usable answer."""


class McpCarrierHttpError(McpExecCarrierError):
    """Сервер MCP ответил HTTP 4xx/5xx — статус привёз носитель.

    Свой класс, а не ``requests.HTTPError``: здесь ядро в сеть не ходит вовсе.
    HTTP случился ВНУТРИ носителя, запущенного в бэкенде задачи, и к нам
    вернулся числом в JSON. Тянуть ради одного имени зависимость от
    ``requests`` — единственное, что держало её в этом модуле.
    """

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


# The container-side script. Doubled braces are literal — this goes through
# str.format. It performs one POST, follows MCP's /mcp -> /mcp/ redirect itself
# (urllib refuses to re-POST on 307/308; it handles 301/302 on its own by
# rewriting the method to GET, which never reaches this loop), stops at the first
# complete JSON-RPC frame instead of reading to EOF, and releases the session once
# the answer is out.
_HTTP_CARRIER = '''
import base64, json, ssl, sys, urllib.error, urllib.parse, urllib.request

_req = json.loads(base64.b64decode("{payload_b64}").decode("utf-8"))
_ctx = ssl._create_unverified_context() if not _req.get("verify_ssl", True) else None  # NOSONAR: verify_ssl из конфига сервера — осознанный opt-in


def _open(request, timeout):
    if _ctx is not None:
        return urllib.request.urlopen(request, timeout=timeout, context=_ctx)
    return urllib.request.urlopen(request, timeout=timeout)


def _read_json_frame(resp):
    """First JSON-RPC object from the body, without waiting for EOF on SSE."""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "text/event-stream" not in ctype:
        raw = resp.read().decode("utf-8", "replace").strip()
        if not raw:
            return {{}}
        try:
            obj = json.loads(raw)
        except ValueError:
            return {{}}
        return obj if isinstance(obj, dict) else {{}}
    buffer = []
    while True:
        line = resp.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").strip()
        if not text:
            buffer = []
            continue
        if not text.startswith("data:"):
            continue
        chunk = text[len("data:"):].strip()
        if not chunk:
            continue
        buffer.append(chunk)
        try:
            obj = json.loads("\\n".join(buffer))
        except ValueError:
            continue
        if isinstance(obj, dict) and ("result" in obj or "error" in obj):
            return obj
        buffer = []
    return {{}}


def _post(url):
    request = urllib.request.Request(
        url,
        data=json.dumps(_req["payload"]).encode("utf-8"),
        headers=_req["headers"],
        method="POST",
    )
    try:
        resp = _open(request, _req["timeout"])
    except urllib.error.HTTPError as exc:
        headers = {{k.lower(): v for k, v in exc.headers.items()}} if exc.headers else {{}}
        try:
            body = _read_json_frame(exc)
        except Exception:
            body = {{}}
        return exc.code, headers, body
    try:
        return (
            resp.getcode(),
            {{k.lower(): v for k, v in resp.headers.items()}},
            _read_json_frame(resp),
        )
    finally:
        resp.close()


def _delete_session(url, session_id):
    """Release the server-side session; best effort, never fails the operation.

    Same headers as the POSTs — a token-protected server answers a bare DELETE
    with 401 and the session would silently outlive the operation.
    """
    try:
        headers = dict(_req["headers"])
        headers["mcp-session-id"] = session_id
        request = urllib.request.Request(url, headers=headers, method="DELETE")
        _open(request, _req["timeout"]).close()
    except Exception:
        pass


try:
    url = _req["url"]
    redirected = False
    for _ in range(4):
        status, headers, body = _post(url)
        location = headers.get("location")
        if status not in (307, 308) or not location:
            redirected = False
            break
        url = urllib.parse.urljoin(url, location)
        redirected = True
    if redirected:
        raise RuntimeError("redirect loop not resolved after 4 hops: " + url)
    session_id = _req["headers"].get("mcp-session-id") or headers.get("mcp-session-id")
except Exception as exc:
    print("{marker}" + json.dumps({{"error": "{{}}: {{}}".format(type(exc).__name__, exc)}}))
else:
    # The answer leaves first. Releasing the session is best effort, and a
    # wedged DELETE must not turn a call that succeeded into a carrier error
    # when the exec deadline lands on it. The flush carries weight: stdout to a
    # pipe is block-buffered, and a killed process takes the envelope with it.
    print("{marker}" + json.dumps({{"status": status, "headers": headers, "body": body}}))
    sys.stdout.flush()
    if _req.get("close_session") and session_id:
        _delete_session(url, session_id)
'''


def _extract_marked(stdout: str) -> Optional[Dict[str, Any]]:
    """Last marked line from the carrier's stdout, parsed."""
    for line in reversed((stdout or "").splitlines()):
        stripped = line.strip()
        if not stripped.startswith(EXEC_RESULT_MARKER):
            continue
        try:
            parsed = json.loads(stripped[len(EXEC_RESULT_MARKER) :])
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


class OsBackendStatelessClient(McpClientPort):
    """`http` / `stream_http` / `stdio`, spoken from inside the backend."""

    def __init__(self, server_name: str, server_cfg: Dict[str, Any], *, os_backend: Any) -> None:
        super().__init__(server_name, server_cfg)
        self.os_backend = os_backend

    _PROBE_COMMAND = "command -v python3 2>/dev/null || true"

    def _probe_timeout(self, timeout: float) -> int:
        return min(_PROBE_TIMEOUT_SEC, max(1, int(timeout)))

    def _remember_probe(self, probe: Any) -> bool:
        """Запомнить исход пробы на бэкенде. Общее для обеих форм."""
        cached = bool((getattr(probe, "stdout", "") or "").strip())
        try:
            setattr(self.os_backend, _CARRIER_CACHE_ATTR, cached)
        except Exception:
            # Backend forbids attributes; probing again is only a lost cache.
            pass
        return cached

    async def _arequire_python3(self, *, timeout: float) -> None:
        """Асинхронный двойник :meth:`_require_python3`."""
        cached = getattr(self.os_backend, _CARRIER_CACHE_ATTR, None)
        if cached is None:
            # Сырой канал: проба носителя — внутренний вызов, профиль образа ей
            # вреден (у develop так же).
            probe = await arun_backend_exec_raw(
                self.os_backend, self._PROBE_COMMAND, timeout=self._probe_timeout(timeout)
            )
            cached = self._remember_probe(probe)
        if not cached:
            raise McpExecCarrierError(self._no_python3_message())

    def _no_python3_message(self) -> str:
        return (
            f"MCP server '{self.server_name}': the environment has no python3 on "
            f"PATH, so an MCP request cannot be carried there. Nothing will be "
            f"installed into the task image; set execute_on: agent for this "
            f"server if it is reachable from the agent."
        )

    # --- асинхронные двойники -------------------------------------------------
    #
    # Весь обмен идёт одним экзеком в бэкенде задачи (`run_python` для HTTP,
    # `exec_argv` для stdio), и у обоих есть асинхронная форма: своя у
    # `local.py`, адаптер `aexec_port` для чужих бэкендов. Поэтому здесь прямой
    # await, а не поток: раньше стоял `run_abandonable`, потому что сквозного
    # `aexec_argv`-пути ещё не было.
    #
    # Отмена работает лучше, чем с потоком: asyncio-подпроцесс по отмене
    # снимается, тогда как брошенный поток-демон дочитывал свой pipe в никуда.

    async def alist_tools(self, *, timeout: float) -> List[Dict[str, Any]]:
        body = await self._aoperation("tools/list", {}, timeout=timeout)
        return _tools_from_body(body)

    async def acall_tool(self, tool_name: str, arguments: Dict[str, Any], *, timeout: float) -> Dict[str, Any]:
        return await self._aoperation("tools/call", {"name": tool_name, "arguments": arguments}, timeout=timeout)

    async def _aoperation(self, method: str, params: Dict[str, Any], *, timeout: float) -> Dict[str, Any]:
        """Асинхронный двойник :meth:`_operation`: порядок шагов общий."""
        if str(self.server_cfg.get("transport") or "") == "stdio":
            return await self._astdio_operation(method, params, timeout=timeout)
        per_request = max(1.0, float(timeout) / _REQUESTS_PER_OPERATION)
        await self._arequire_python3(timeout=per_request)
        session_id = await self._ahandshake(timeout=per_request)
        _headers, body = await self._arequest(
            {"jsonrpc": "2.0", "id": f"gc-{method}", "method": method, "params": params},
            session_id=session_id,
            timeout=per_request,
            close_session=True,
        )
        return body

    async def _ahandshake(self, *, timeout: float) -> Optional[str]:
        headers, _body = await self._arequest(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": MCP_INIT_PARAMS},
            session_id=None,
            timeout=timeout,
        )
        session_id = headers.get("mcp-session-id")
        await self._arequest(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=session_id,
            timeout=timeout,
        )
        return session_id

    def _carrier_code(
        self,
        payload: Dict[str, Any],
        *,
        session_id: Optional[str],
        timeout: float,
        close_session: bool,
    ) -> Tuple[str, int]:
        """Собрать python-носитель одного запроса. Общее для обеих форм.

        Возвращает ``(код, дедлайн)``: сокетный таймаут внутри носителя и
        дедлайн процесса — разные величины, и делит их ``_split_share``.
        """
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["mcp-session-id"] = session_id
        token = self.server_cfg.get("token")
        headers.update(
            _merge_mcp_headers(
                token=str(token) if token else None,
                extra_headers=self.server_cfg.get("headers") or {},
            )
        )
        # A CA path is rejected at resolve time, so this is a plain on/off flag.
        verify = get_verify_ssl(self.server_cfg)
        request = {
            "url": _normalize_url(str(self.server_cfg.get("url") or "")),
            "headers": headers,
            "payload": payload,
            "timeout": float(timeout),
            "verify_ssl": bool(verify),
            "close_session": bool(close_session),
        }
        socket_timeout, deadline = _split_share(timeout)
        request["timeout"] = socket_timeout
        payload_b64 = base64.b64encode(json.dumps(request).encode("utf-8")).decode("ascii")
        code = _HTTP_CARRIER.format(payload_b64=payload_b64, marker=EXEC_RESULT_MARKER)
        return code, deadline

    def _carrier_argv(self) -> list:
        """Как запускать носитель. ``python3 -`` со скриптом на stdin.

        Тот же канал, которым пользуется stdio-транспорт, а не модельный путь
        ``run_python``: у последнего свой потолок вывода, рассчитанный на
        трассу тула, и крупный ответ MCP он обрезал.
        """
        return ["python3", "-"]

    async def _arequest(
        self,
        payload: Dict[str, Any],
        *,
        session_id: Optional[str],
        timeout: float,
        close_session: bool = False,
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
        """Асинхронный двойник: сборка носителя и разбор ответа общие."""
        code, deadline = self._carrier_code(
            payload,
            session_id=session_id,
            timeout=timeout,
            close_session=close_session,
        )
        result = await aexec_port(self.os_backend).aexec_argv(
            self._carrier_argv(),
            stdin=code,
            timeout=deadline,
            capture_max_chars=self._carrier_output_max_chars(),
        )
        return self._interpret(result)

    def _stdio_call_parts(self, method: str, params: Dict[str, Any]) -> Tuple[list, str, str]:
        """``(argv, stdin, rpc_id)`` одного stdio-обмена. Общее для обеих форм.

        MCP stdio-серверы читают JSON-RPC построчно, поэтому все три сообщения
        пишутся сразу; закрытие stdin завершает процесс. Ответ ищется по id —
        сервер вправе вперемешку слать уведомления.
        """
        command = self.server_cfg.get("command")
        if not command:
            raise McpExecCarrierError(
                f"MCP server '{self.server_name}': transport stdio needs a 'command' in the config"
            )
        argv = [str(command)] + [str(a) for a in (self.server_cfg.get("args") or [])]
        env = {str(k): str(v) for k, v in (self.server_cfg.get("env") or {}).items()}
        if env:
            # `exec_argv` builds `printf … | <argv>` and backends prepend `env K=V`
            # to the whole string — in POSIX grammar the pipe splits the command
            # first, so the prefix would land on printf. Put `env` in the argv of
            # the right side instead, where it binds to the server process.
            argv = ["env"] + [f"{k}={v}" for k, v in env.items()] + argv
        rpc_id = f"gc-{method}"
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": MCP_INIT_PARAMS},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params},
        ]
        stdin = "".join(json.dumps(m) + "\n" for m in messages)
        return argv, stdin, rpc_id

    def _stdio_reply(self, result: Any, rpc_id: str) -> Dict[str, Any]:
        """Разбор вывода stdio-обмена. Общее для обеих форм."""
        if getattr(result, "stdout_hit_capture_cap", False):
            raise McpExecCarrierError(
                f"MCP server '{self.server_name}': response truncated by the exec "
                f"output cap; the answer is too large to carry through exec"
            )
        for line in (getattr(result, "stdout", "") or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                frame = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict) and frame.get("id") == rpc_id:
                return frame
        stderr = (getattr(result, "stderr", "") or "")[:500]
        raise McpExecCarrierError(
            f"MCP server '{self.server_name}': no reply for id {rpc_id!r} in the "
            f"stdio output (rc={getattr(result, 'return_code', None)}, "
            f"stderr={stderr})"
        )

    async def _astdio_operation(self, method: str, params: Dict[str, Any], *, timeout: float) -> Dict[str, Any]:
        """Асинхронный двойник: сборка и разбор общие, отличается запуск."""
        argv, stdin, rpc_id = self._stdio_call_parts(method, params)
        result = await aexec_port(self.os_backend).aexec_argv(
            argv,
            stdin=stdin,
            cwd=self.server_cfg.get("cwd"),
            timeout=max(1, int(timeout)),
            capture_max_chars=self._carrier_output_max_chars(),
        )
        return self._stdio_reply(result, rpc_id)

    def _carrier_output_max_chars(self) -> int:
        """The MCP answer is transport, not a model-facing trace: it is parsed and
        then bounded by ``tool_output_inline_max_chars`` like any tool output. So
        it rides the backend's internal-channel ceiling (the one walk/listdir
        use), not the 60k shell cap — a product catalog must not fail the call.
        Backends without the attribute (local) get the protocol default."""
        return int(getattr(self.os_backend, "walk_output_max_chars", WALK_OUTPUT_MAX_CHARS))

    def _interpret(self, result: Any) -> Tuple[Dict[str, str], Dict[str, Any]]:
        if getattr(result, "stdout_hit_capture_cap", False):
            raise McpExecCarrierError(
                f"MCP server '{self.server_name}': response truncated by the exec "
                f"output cap; the answer is too large to carry through exec"
            )
        parsed = _extract_marked(getattr(result, "stdout", "") or "")
        if parsed is None:
            stderr = (getattr(result, "stderr", "") or "")[:500]
            raise McpExecCarrierError(
                f"MCP server '{self.server_name}': no carrier result in stdout "
                f"(rc={getattr(result, 'return_code', None)}, stderr={stderr})"
            )
        if "error" in parsed and "status" not in parsed:
            raise McpExecCarrierError(f"MCP server '{self.server_name}': carrier failed: {parsed['error']}")
        status = int(parsed.get("status") or 0)
        if status >= 400:
            raise McpCarrierHttpError(
                f"MCP server '{self.server_name}': HTTP {status} from {self.server_cfg.get('url')}",
                status=status,
            )
        headers = {str(k).lower(): str(v) for k, v in (parsed.get("headers") or {}).items()}
        body = parsed.get("body")
        return headers, (body if isinstance(body, dict) else {})


__all__ = [
    "EXEC_RESULT_MARKER",
    "McpExecCarrierError",
    "OsBackendStatelessClient",
]
