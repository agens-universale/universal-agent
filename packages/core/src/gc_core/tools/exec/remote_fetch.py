"""HTTP GET через бэкенд: сеть у среды исполнения, разбор ответа — на хосте.

Для `exec_fetch_url` с `os_backend.fetch_url_execute_on: os_backend`. Один
переход = один запуск stdlib-скрипта на `python3` той стороны (как MCP-carrier
в ``connect_mcp/os_backend_client.py``): скрипт делает GET без автоследования
редиректам и печатает статус, заголовки и тело (base64) одним JSON. Редиректы
разворачивает хост — каждый hop проходит `url_policy`, как и на хостовом пути.

Результат собирается в настоящий ``httpx.Response``: дальше тул не отличает его
от ответа своего клиента (``.text``, ``.headers``, ``raise_for_status``).
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, Optional, Sequence

import httpx

from gc_core.backends.async_protocol import aexec_port
from gc_core.security.url_policy import validate_url_policy
from gc_core.tools.exec.redirect_policy import (
    DEFAULT_MAX_REDIRECTS,
    RedirectBlocked,
    TooManyRedirects,
    _next_hop,
)

RESULT_MARKER = "__GC_FETCH__"

#: Потолок тела одного ответа: страницу для чтения больше не имеет смысла
#: тянуть через base64 по трубе. Хвост отбрасывается, ответ помечается
#: заголовком ``X-GC-Truncated``.
MAX_BODY_BYTES = 8 * 1024 * 1024

#: Запас поверх HTTP-таймаута на старт python3 и передачу тела.
_EXEC_SLACK_SEC = 15

_CARRIER = r"""
import base64, json, sys, urllib.request, urllib.error
P = json.loads(__PARAMS__)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

handlers = [_NoRedirect()]
if P["proxy"]:
    handlers.append(urllib.request.ProxyHandler({"http": P["proxy"], "https": P["proxy"]}))
opener = urllib.request.build_opener(*handlers)
req = urllib.request.Request(P["url"], headers=P["headers"], method="GET")
limit = P["max_bytes"]

def _pack(status, headers, body, url):
    truncated = len(body) > limit
    return {"status": status, "headers": list(headers), "truncated": truncated,
            "body": base64.b64encode(body[:limit]).decode("ascii"), "url": url}

try:
    r = opener.open(req, timeout=P["timeout"])
    out = _pack(r.status, r.getheaders(), r.read(limit + 1), r.geturl())
except urllib.error.HTTPError as e:
    out = _pack(e.code, e.headers.items(), e.read(limit + 1), P["url"])
except Exception as e:
    out = {"error": type(e).__name__ + ": " + str(e)}
sys.stdout.write("__GC_FETCH__" + json.dumps(out))
"""


def build_carrier(params: Dict[str, Any]) -> str:
    """Скрипт для `python3 -` с параметрами, вшитыми JSON-литералом."""
    return _CARRIER.replace("__PARAMS__", json.dumps(json.dumps(params)))


def _request_error(message: str, url: str) -> httpx.HTTPError:
    req = httpx.Request("GET", url)
    if "timed out" in message or "TimeoutError" in message:
        return httpx.ReadTimeout(message, request=req)
    return httpx.ConnectError(message, request=req)


def _parse_carrier_output(res: Any, url: str) -> Dict[str, Any]:
    stdout = res.stdout or ""
    marker = stdout.rfind(RESULT_MARKER)
    if marker < 0:
        detail = (res.stderr or "").strip() or f"exit code {res.return_code}"
        if "python3" in detail and "not found" in detail:
            detail = "the execution environment has no python3 on PATH, exec_fetch_url cannot run there; set os_backend.fetch_url_execute_on: agent"
        if getattr(res, "timed_out", False):
            detail = f"carrier timed out: {detail}"
        raise _request_error(f"remote fetch failed: {detail}", url)
    try:
        out = json.loads(stdout[marker + len(RESULT_MARKER) :])
    except ValueError as exc:
        raise _request_error(f"remote fetch returned garbage: {exc}", url)
    if "error" in out:
        raise _request_error(out["error"], url)
    return out


async def aone_hop(
    backend: Any,
    url: str,
    headers: Dict[str, str],
    timeout: float,
    proxy: Optional[str],
    *,
    max_bytes: int = MAX_BODY_BYTES,
) -> httpx.Response:
    """Один GET без следования редиректам, выполненный на стороне бэкенда."""
    params = {
        "url": url,
        "headers": dict(headers),
        "timeout": float(timeout),
        "proxy": proxy or "",
        "max_bytes": int(max_bytes),
    }
    # base64 тела + заголовки; cap выше потолка, чтобы обрезал скрипт, а не труба.
    cap = max_bytes * 4 // 3 + 64 * 1024
    res = await aexec_port(backend).arun_python(
        build_carrier(params),
        timeout=int(timeout) + _EXEC_SLACK_SEC,
        capture_max_chars=cap,
    )
    out = _parse_carrier_output(res, url)
    response_headers = [(str(k), str(v)) for k, v in out["headers"]]
    if out.get("truncated"):
        response_headers.append(("X-GC-Truncated", "1"))
    return httpx.Response(
        int(out["status"]),
        headers=response_headers,
        content=base64.b64decode(out["body"]),
        request=httpx.Request("GET", out.get("url") or url),
    )


async def afetch_via_backend(
    backend: Any,
    start_url: str,
    headers: Dict[str, str],
    timeout: float,
    proxy: Optional[str],
    *,
    allowed_schemes: Sequence[str],
    url_blacklist: Sequence[str],
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> httpx.Response:
    """Аналог ``fetch_validating_redirects`` с сетью на стороне бэкенда."""
    current = start_url
    for _ in range(max_redirects + 1):
        response = await aone_hop(backend, current, headers, timeout, proxy)
        next_abs = _next_hop(response, current)
        if next_abs is None:
            return response
        decision = validate_url_policy(
            next_abs, allowed_schemes, url_blacklist
        )
        if not decision.allowed:
            raise RedirectBlocked(decision)
        current = next_abs
    raise TooManyRedirects(current, max_redirects)


def fetches_on_backend(settings: Any, backend: Any) -> bool:
    """Качать через бэкенд: так настроено и бэкенд — не хост.

    ``settings`` — всё, что принимает ``settings_of``; ``None`` — на хосте.
    """
    if settings is None:
        return False
    from gc_core.settings import settings_of

    if settings_of(settings).os_backend.fetch_url_execute_on != "os_backend":
        return False
    return bool(getattr(backend, "is_sandbox", False))


__all__ = [
    "MAX_BODY_BYTES",
    "RESULT_MARKER",
    "afetch_via_backend",
    "aone_hop",
    "build_carrier",
    "fetches_on_backend",
]
