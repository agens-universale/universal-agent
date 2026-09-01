"""One MCP operation = one stateless exchange inside the tool-layer environment.

Nothing is installed in the image, no file is written, no process is left
running. Each agent-level operation (`tools/list`, `tools/call`) carries its own
`initialize` -> `notifications/initialized` -> method handshake and throws the
session away afterwards; the sim-user sidecars this exists for keep their state
in the server process, not in the MCP session.

The script that runs in the container is stdlib-only and does the minimum
framing work needed to know when to stop reading: an SSE answer must not be read
to EOF, because per MCP Streamable HTTP the server only SHOULD close the stream
after the final result.
"""
import base64
import json
import logging
import shlex
from typing import Any, Dict, List, Optional, Tuple

import requests

from tools.connect_mcp.client import _merge_mcp_headers, _normalize_url
from tools.connect_mcp.client_port import McpClientPort

logger = logging.getLogger(__name__)

EXEC_RESULT_MARKER = "__GC_MCP_EXEC__"

# The exec channel caps captured stdout (backends/protocol.py:10). Leave the
# backend's own limit in charge; we only detect the hit and fail loudly.
_TIMEOUT_SLACK_SEC = 15

# Carrier order is deliberate and differs from the spec's `curl -> wget ->
# python3`. FastMCP may answer tools/call with text/event-stream, and per MCP
# Streamable HTTP the server only SHOULD close that stream after the final
# result. A carrier that waits for EOF (curl, wget) hangs or truncates; only the
# python3 carrier stops at the first complete frame. python3 is present in every
# task image we target, so it leads; the shell carriers stay as fallbacks and
# always carry an explicit timeout so a stuck stream fails instead of hanging.
_CARRIERS = ("python3", "curl", "wget")

_INIT_PARAMS: Dict[str, Any] = {
    "protocolVersion": "2024-11-05",
    "capabilities": {"roots": {"listChanged": True}, "sampling": {}},
    "clientInfo": {"name": "SurfaceClient", "version": "1.0.0"},
}


class McpExecCarrierError(RuntimeError):
    """The exec carrier could not deliver a usable answer."""


# The container-side script. Doubled braces are literal — this goes through
# str.format. It performs one POST, follows MCP's /mcp -> /mcp/ redirect itself
# (urllib will not re-POST on 307), and stops at the first complete JSON-RPC
# frame instead of reading to EOF.
_HTTP_CARRIER = '''
import base64, json, urllib.error, urllib.parse, urllib.request

_req = json.loads(base64.b64decode("{payload_b64}").decode("utf-8"))


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
        resp = urllib.request.urlopen(request, timeout=_req["timeout"])
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


try:
    url = _req["url"]
    for _ in range(4):
        status, headers, body = _post(url)
        location = headers.get("location")
        if status not in (301, 302, 307, 308) or not location:
            break
        url = urllib.parse.urljoin(url, location)
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

    _carrier_name: Optional[str] = None

    def __init__(
        self, server_name: str, server_cfg: Dict[str, Any], *, os_backend: Any
    ) -> None:
        super().__init__(server_name, server_cfg)
        self.os_backend = os_backend

    def _carrier(self) -> str:
        """Which HTTP client the environment can offer. Probed once per client."""
        if self._carrier_name is not None:
            return self._carrier_name
        for name in _CARRIERS:
            probe = self.os_backend.exec(
                f"command -v {name} 2>/dev/null || true", timeout=20
            )
            if (getattr(probe, "stdout", "") or "").strip():
                self._carrier_name = name
                return name
        raise McpExecCarrierError(
            f"MCP server '{self.server_name}': the environment has none of "
            f"python3, curl, wget on PATH, so an HTTP request cannot be carried "
            f"there. Nothing will be installed into the task image; set "
            f"execute_on: agent for this server if it is reachable from the agent."
        )

    def _shell_carrier_command(self, request: Dict[str, Any]) -> str:
        """curl/wget fallback. Correct for application/json answers; an SSE answer
        the server leaves open is cut by the explicit timeout rather than hanging
        the run.

        This path reports neither the real HTTP status nor the response headers —
        the envelope is assembled from the body alone, so `mcp-session-id` is
        unavailable and a 4xx/5xx surfaces as an empty body rather than an
        HTTPError. Acceptable here: a server that omits the session header still
        answers a stateless handshake, an empty body already fails loudly one
        layer up, and this carrier only runs where python3 is absent.
        """
        body = json.dumps(request["payload"])
        timeout_s = int(request["timeout"])
        url = shlex.quote(request["url"])
        if self._carrier() == "curl":
            header_args = " ".join(
                f"-H {shlex.quote(f'{k}: {v}')}" for k, v in request["headers"].items()
            )
            fetch = (
                f"curl -sS -L --max-time {timeout_s} -X POST {header_args} "
                f"--data-binary {shlex.quote(body)} {url}"
            )
        else:
            header_args = " ".join(
                f"--header={shlex.quote(f'{k}: {v}')}"
                for k, v in request["headers"].items()
            )
            fetch = (
                f"wget -q -O - --timeout={timeout_s} {header_args} "
                f"--post-data={shlex.quote(body)} {url}"
            )
        marker = shlex.quote(EXEC_RESULT_MARKER)
        return (
            f"{fetch} > /tmp/gc_mcp_body.$$ 2>/dev/null; "
            f"printf '%s' {marker}; "
            f"printf '{{\"status\": 200, \"headers\": {{}}, \"body\": '; "
            f"cat /tmp/gc_mcp_body.$$; printf '}}\\n'; "
            f"rm -f /tmp/gc_mcp_body.$$"
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
        """Handshake plus the method, then drop the session on the floor."""
        session_id = self._handshake(timeout=timeout)
        _headers, body = self._request(
            {"jsonrpc": "2.0", "id": f"gc-{method}", "method": method,
             "params": params},
            session_id=session_id,
            timeout=timeout,
        )
        return body

    def _handshake(self, *, timeout: float) -> Optional[str]:
        headers, _body = self._request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": _INIT_PARAMS},
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
        request = {
            "url": _normalize_url(str(self.server_cfg.get("url") or "")),
            "headers": headers,
            "payload": payload,
            "timeout": float(timeout),
        }
        payload_b64 = base64.b64encode(
            json.dumps(request).encode("utf-8")
        ).decode("ascii")
        code = _HTTP_CARRIER.format(
            payload_b64=payload_b64, marker=EXEC_RESULT_MARKER
        )
        # Give the backend kill a margin over the in-script socket timeout, so a
        # slow answer surfaces as an MCP timeout rather than an opaque exec one.
        deadline = int(timeout) + _TIMEOUT_SLACK_SEC
        if self._carrier() == "python3":
            result = self.os_backend.run_python(code, timeout=deadline)
        else:
            result = self.os_backend.exec(
                self._shell_carrier_command(request), timeout=deadline
            )
        return self._interpret(result)

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
