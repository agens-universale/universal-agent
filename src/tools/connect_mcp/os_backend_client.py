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
from typing import Any, Dict, List, Optional, Tuple

import requests

from tools.connect_mcp.client import _merge_mcp_headers, _normalize_url
from tools.connect_mcp.client_port import McpClientPort

logger = logging.getLogger(__name__)

EXEC_RESULT_MARKER = "__GC_MCP_EXEC__"

# The exec channel caps captured stdout (backends/protocol.py:10). Leave the
# backend's own limit in charge; we only detect the hit and fail loudly.
_TIMEOUT_SLACK_SEC = 15

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

    def __init__(
        self, server_name: str, server_cfg: Dict[str, Any], *, os_backend: Any
    ) -> None:
        super().__init__(server_name, server_cfg)
        self.os_backend = os_backend

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
        result = self.os_backend.run_python(
            code, timeout=int(timeout) + _TIMEOUT_SLACK_SEC
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
