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

import requests

from tools.connect_mcp.client import (
    MCP_INIT_PARAMS,
    _merge_mcp_headers,
    _normalize_url,
)
from tools.connect_mcp.client_port import McpClientPort
from utils.ssl_config import get_verify_ssl

logger = logging.getLogger(__name__)

EXEC_RESULT_MARKER = "__GC_MCP_EXEC__"

# Margin between the in-script socket timeout and the backend's own kill, so a
# slow answer surfaces as an MCP timeout rather than an opaque exec timeout.
_TIMEOUT_SLACK_SEC = 15

# One operation is three requests (initialize, initialized, method). The caller's
# timeout is the budget for the whole operation, so each request gets a share —
# otherwise a single operation can outlive the discovery deadline that granted it.
_REQUESTS_PER_OPERATION = 3

# Attribute the python3 probe is cached under, on the backend rather than on the
# client: carrier availability is a property of the environment, and clients are
# rebuilt per operation.
_CARRIER_CACHE_ATTR = "_gc_mcp_has_python3"


class McpExecCarrierError(RuntimeError):
    """The exec carrier could not deliver a usable answer."""


# The container-side script. Doubled braces are literal — this goes through
# str.format. It performs one POST, follows MCP's /mcp -> /mcp/ redirect itself
# (urllib refuses to re-POST on 307/308; it handles 301/302 on its own by
# rewriting the method to GET, which never reaches this loop), stops at the first
# complete JSON-RPC frame instead of reading to EOF, and releases the session.
_HTTP_CARRIER = '''
import base64, json, ssl, urllib.error, urllib.parse, urllib.request

_req = json.loads(base64.b64decode("{payload_b64}").decode("utf-8"))
_ctx = ssl._create_unverified_context() if not _req.get("verify_ssl", True) else None


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
    """Release the server-side session; best effort, never fails the operation."""
    try:
        request = urllib.request.Request(
            url, headers={{"mcp-session-id": session_id}}, method="DELETE"
        )
        _open(request, 5).close()
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
    if _req.get("close_session") and session_id:
        _delete_session(url, session_id)
except Exception as exc:
    print("{marker}" + json.dumps({{"error": "{{}}: {{}}".format(type(exc).__name__, exc)}}))
else:
    print("{marker}" + json.dumps({{"status": status, "headers": headers, "body": body}}))
'''


def _extract_marked(stdout: str) -> Optional[Dict[str, Any]]:
    """Last marked line from the carrier's stdout, parsed."""
    for line in reversed((stdout or "").splitlines()):
        stripped = line.strip()
        if not stripped.startswith(EXEC_RESULT_MARKER):
            continue
        try:
            parsed = json.loads(stripped[len(EXEC_RESULT_MARKER):])
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


class OsBackendStatelessClient(McpClientPort):
    """`http` / `stream_http` / `stdio`, spoken from inside the backend."""

    def __init__(
        self, server_name: str, server_cfg: Dict[str, Any], *, os_backend: Any
    ) -> None:
        super().__init__(server_name, server_cfg)
        self.os_backend = os_backend

    def _require_python3(self, *, timeout: float) -> None:
        """Probe once per backend: the carrier is an environment property."""
        cached = getattr(self.os_backend, _CARRIER_CACHE_ATTR, None)
        if cached is None:
            probe = self.os_backend.exec(
                "command -v python3 2>/dev/null || true", timeout=max(5, int(timeout))
            )
            cached = bool((getattr(probe, "stdout", "") or "").strip())
            try:
                setattr(self.os_backend, _CARRIER_CACHE_ATTR, cached)
            except Exception:
                # Backend forbids attributes; probing again is only a lost cache.
                pass
        if not cached:
            raise McpExecCarrierError(
                f"MCP server '{self.server_name}': the environment has no python3 on "
                f"PATH, so an MCP request cannot be carried there. Nothing will be "
                f"installed into the task image; set execute_on: agent for this "
                f"server if it is reachable from the agent."
            )

    # --- public port ---

    def list_tools(self, *, timeout: float) -> List[Dict[str, Any]]:
        body = self._operation("tools/list", {}, timeout=timeout)
        result = body.get("result") if isinstance(body, dict) else None
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return tools if isinstance(tools, list) else []

    def call_tool(
        self, tool_name: str, arguments: Dict[str, Any], *, timeout: float
    ) -> Dict[str, Any]:
        return self._operation(
            "tools/call", {"name": tool_name, "arguments": arguments}, timeout=timeout
        )

    # --- one stateless operation ---

    def _operation(
        self, method: str, params: Dict[str, Any], *, timeout: float
    ) -> Dict[str, Any]:
        """Handshake, the method, then release the session on both sides."""
        if str(self.server_cfg.get("transport") or "") == "stdio":
            return self._stdio_operation(method, params, timeout=timeout)
        # The caller's timeout covers the whole operation, not one request.
        per_request = max(1.0, float(timeout) / _REQUESTS_PER_OPERATION)
        self._require_python3(timeout=per_request)
        session_id = self._handshake(timeout=per_request)
        _headers, body = self._request(
            {"jsonrpc": "2.0", "id": f"gc-{method}", "method": method,
             "params": params},
            session_id=session_id,
            timeout=per_request,
            close_session=True,
        )
        return body

    def _handshake(self, *, timeout: float) -> Optional[str]:
        headers, _body = self._request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": MCP_INIT_PARAMS},
            session_id=None,
            timeout=timeout,
        )
        session_id = headers.get("mcp-session-id")
        self._request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            session_id=session_id,
            timeout=timeout,
        )
        return session_id

    def _request(
        self,
        payload: Dict[str, Any],
        *,
        session_id: Optional[str],
        timeout: float,
        close_session: bool = False,
    ) -> Tuple[Dict[str, str], Dict[str, Any]]:
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
        payload_b64 = base64.b64encode(
            json.dumps(request).encode("utf-8")
        ).decode("ascii")
        code = _HTTP_CARRIER.format(
            payload_b64=payload_b64, marker=EXEC_RESULT_MARKER
        )
        result = self.os_backend.run_python(
            code, timeout=int(timeout) + _TIMEOUT_SLACK_SEC
        )
        return self._interpret(result)

    def _stdio_operation(
        self, method: str, params: Dict[str, Any], *, timeout: float
    ) -> Dict[str, Any]:
        """One process, one exchange: handshake and the method share the pipe.

        MCP stdio servers read newline-delimited JSON-RPC, so all three messages
        can be written at once; closing stdin then ends the process. The reply is
        matched by id because servers may interleave notifications.
        """
        command = self.server_cfg.get("command")
        if not command:
            raise McpExecCarrierError(
                f"MCP server '{self.server_name}': transport stdio needs a "
                f"'command' in the config"
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
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": MCP_INIT_PARAMS},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params},
        ]
        stdin = "".join(json.dumps(m) + "\n" for m in messages)
        result = self.os_backend.exec_argv(
            argv,
            stdin=stdin,
            cwd=self.server_cfg.get("cwd"),
            timeout=int(timeout) + _TIMEOUT_SLACK_SEC,
        )
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
            raise McpExecCarrierError(
                f"MCP server '{self.server_name}': carrier failed: {parsed['error']}"
            )
        status = int(parsed.get("status") or 0)
        if status >= 400:
            raise requests.HTTPError(
                f"MCP server '{self.server_name}': HTTP {status} from "
                f"{self.server_cfg.get('url')}"
            )
        headers = {
            str(k).lower(): str(v) for k, v in (parsed.get("headers") or {}).items()
        }
        body = parsed.get("body")
        return headers, (body if isinstance(body, dict) else {})


__all__ = [
    "EXEC_RESULT_MARKER",
    "McpExecCarrierError",
    "OsBackendStatelessClient",
]
