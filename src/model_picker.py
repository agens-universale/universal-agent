"""Список моделей с эндпоинта: /config (gigacode) → /models (стандарт) → None.
HTTP инъектируется (http_get) → тестируется без сети."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class ModelInfo:
    id: str
    description: str = ""
    tool_capable: Optional[bool] = None   # None = неизвестно (нет ⚠)
    context: Optional[int] = None


@dataclass(frozen=True)
class HttpReply:
    """Ответ одного HTTP-вызова probe-путей: код ЛИБО сетевая ошибка."""
    status: Optional[int]          # None = до сервера не достучались
    payload: Optional[dict]
    network_error: str = ""


@dataclass(frozen=True)
class FetchOutcome:
    models: Optional[list]
    status: Optional[int]
    network_error: str
    latency_ms: int


def parse_config_models(payload: dict) -> list:
    models = (((payload or {}).get("chat") or {}).get("models")) or []
    out: list = []
    for m in models:
        if not isinstance(m, dict) or not m.get("model"):
            continue
        ps = m.get("plugin_side") or {}
        tc = ps.get("supports_parallel_tool_calls")
        out.append(ModelInfo(
            id=str(m["model"]),
            description=str(m.get("description") or ""),
            tool_capable=tc if isinstance(tc, bool) else None,
            context=ps.get("context_window_size") if isinstance(ps.get("context_window_size"), int) else None,
        ))
    return out


def parse_models_list(payload: dict) -> list:
    data = (payload or {}).get("data") or []
    out: list = []
    for m in data:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        eps = m.get("endpoints")
        if isinstance(eps, list) and "/chat/completions" not in eps:
            continue   # есть список эндпоинтов и chat/completions нет → пропуск
        out.append(ModelInfo(id=str(m["id"])))
    return out


def api_base(completions_url: str) -> str:
    u = (completions_url or "").rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if u.endswith(suffix):
            return u[: -len(suffix)]
    return u


def _urllib_get_ex(url: str, headers: dict, verify: bool) -> HttpReply:
    import socket
    import ssl
    import urllib.error
    import urllib.request
    ctx = None if verify else ssl._create_unverified_context()
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            try:
                payload = json.loads(resp.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = None
            return HttpReply(status=resp.status, payload=payload)
    except urllib.error.HTTPError as exc:
        return HttpReply(status=exc.code, payload=None)
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
        return HttpReply(status=None, payload=None, network_error=str(exc) or exc.__class__.__name__)


def _urllib_post_ex(url: str, headers: dict, body: dict, verify: bool) -> HttpReply:
    import socket
    import ssl
    import urllib.error
    import urllib.request
    ctx = None if verify else ssl._create_unverified_context()
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            try:
                payload = json.loads(resp.read().decode("utf-8"))
            except ValueError:
                payload = None
            return HttpReply(status=resp.status, payload=payload)
    except urllib.error.HTTPError as exc:
        return HttpReply(status=exc.code, payload=None)
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError, ValueError) as exc:
        return HttpReply(status=None, payload=None, network_error=str(exc) or exc.__class__.__name__)


def probe_fetch(completions_url: str, auth_header: Optional[str], *,
                verify: bool = True, http_get_ex: Optional[Callable] = None) -> FetchOutcome:
    """models-цепочка (/config → /models) с кодами. 401 приоритетнее прочих кодов."""
    import time
    get_ex = http_get_ex or _urllib_get_ex
    base = api_base(completions_url)
    headers = {"Content-Type": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header
    t0 = time.monotonic()
    if not base:
        return FetchOutcome(None, None, "пустой адрес", 0)
    statuses: list = []
    net_errors: list = []
    for path, parser in (("/config", parse_config_models), ("/models", parse_models_list)):
        reply = get_ex(base + path, headers, verify)
        if reply.status is not None and reply.payload is not None:
            try:
                parsed = parser(reply.payload)
            except Exception:
                parsed = []
            if parsed:
                return FetchOutcome(parsed, reply.status, "",
                                    int((time.monotonic() - t0) * 1000))
        if reply.status is not None:
            statuses.append(reply.status)
        else:
            net_errors.append(reply.network_error)
    latency = int((time.monotonic() - t0) * 1000)
    if 401 in statuses:
        return FetchOutcome(None, 401, "", latency)
    if statuses:
        return FetchOutcome(None, statuses[-1], "", latency)
    return FetchOutcome(None, None, "; ".join(dict.fromkeys(net_errors)) or "нет соединения", latency)


def probe_post(completions_url: str, auth_header: Optional[str], model: str, *,
               verify: bool = True, http_post_ex: Optional[Callable] = None) -> tuple[Optional[int], str, int]:
    """Минимальный POST в сам chat/completions (ступень 2 probe). → (status, net_err, latency_ms)."""
    import time
    post_ex = http_post_ex or _urllib_post_ex
    headers = {"Content-Type": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header
    body = {"model": model, "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1, "stream": False}
    t0 = time.monotonic()
    reply = post_ex(completions_url, headers, body, verify)
    return reply.status, reply.network_error, int((time.monotonic() - t0) * 1000)


def _urllib_get(url: str, headers: dict, verify: bool) -> dict:
    import ssl
    import urllib.request
    ctx = None if verify else ssl._create_unverified_context()
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_models(completions_url: str, auth_header: Optional[str], *,
                 verify: bool = True, http_get: Optional[Callable] = None):
    get = http_get or _urllib_get
    base = api_base(completions_url)
    if not base:
        return None
    headers = {"Content-Type": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header
    try:
        cfg = get(base + "/config", headers, verify)
        parsed = parse_config_models(cfg)
        if parsed:
            return parsed
    except Exception:
        pass
    try:
        lst = get(base + "/models", headers, verify)
        return parse_models_list(lst)
    except Exception:
        pass
    return None
