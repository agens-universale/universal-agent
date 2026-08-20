"""Список моделей с эндпоинта: /config (gigacode) → /models (стандарт) → None.
HTTP инъектируется (http_get) → тестируется без сети.

Дефолтный транспорт — requests Session с trust_env=False и тем же
resolve_proxies, что у backend (config.proxy / env / NO_PROXY).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Optional


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


@lru_cache(maxsize=1)
def _user_agent() -> str:
    """Визитка продукта из метаданных пакета — имя и версия не задаются здесь."""
    from gc_core import app_version
    return f"{app_version.DIST_NAME}/{app_version.agent_version()}"


def _request(url: str, headers: dict, *, data=None, method=None):
    """urllib-запрос, представленный по имени. Без User-Agent Python шлёт
    `Python-urllib/X.Y`, и щит провайдера (Cloudflare у Groq — проверено живьём)
    отвечает 403 ещё ДО проверки токена: с рабочим ключом список моделей молчал бы.
    Дефолт идёт первым — заголовок вызывающего его перекрывает."""
    import urllib.request
    return urllib.request.Request(
        url, data=data, method=method,
        headers={"User-Agent": _user_agent(), **headers})

def _headers(auth_header: Optional[str]) -> dict:
    """Заголовки запроса: тип, визитка продукта, авторизация.

    Визитка обязательна и ставится здесь, а не в транспорте: транспортов теперь два
    (requests-сессия backend'а и urllib-фолбэк), и без User-Agent библиотека шлёт свой
    дефолт («python-requests/X.Y», «Python-urllib/X.Y») — щит провайдера (Cloudflare у
    Groq, проверено живьём) отвечает 403 ещё ДО проверки токена, и список моделей
    молчит на рабочем ключе."""
    headers = {"Content-Type": "application/json", "User-Agent": _user_agent()}
    if auth_header:
        headers["Authorization"] = auth_header
    return headers


def _payload_dict(raw) -> Optional[dict]:
    return raw if isinstance(raw, dict) else None


def _requests_get_ex(
    url: str,
    headers: dict,
    verify: bool,
    *,
    config_proxy: Optional[str] = None,
) -> HttpReply:
    """GET через backend-сессию: trust_env=False + resolve_proxies."""
    import requests
    from gc_core.utils.http_proxy import resolve_proxies
    from gc_core.utils.http_session import NO_NETRC_SESSION

    proxies = resolve_proxies(config_proxy, os.environ, url)
    try:
        resp = NO_NETRC_SESSION.get(
            url, headers=headers, verify=verify, timeout=5, proxies=proxies,
        )
        try:
            payload = _payload_dict(resp.json())
        except ValueError:
            payload = None
        return HttpReply(status=resp.status_code, payload=payload)
    # UnicodeEncodeError — не сеть: заголовки кодируются в latin-1, и токен с
    # кириллицей рвёт запрос в клиенте. Это ValueError, мимо RequestException, —
    # без перехвата исключение уходит наверх и там читается как «сервер недоступен».
    except (requests.exceptions.RequestException, UnicodeEncodeError) as exc:
        return HttpReply(
            status=None,
            payload=None,
            network_error=str(exc) or exc.__class__.__name__,
        )


def _requests_post_ex(
    url: str,
    headers: dict,
    body: dict,
    verify: bool,
    *,
    config_proxy: Optional[str] = None,
) -> HttpReply:
    """POST через backend-сессию: trust_env=False + resolve_proxies."""
    import requests
    from gc_core.utils.http_proxy import resolve_proxies
    from gc_core.utils.http_session import NO_NETRC_SESSION

    proxies = resolve_proxies(config_proxy, os.environ, url)
    try:
        resp = NO_NETRC_SESSION.post(
            url, headers=headers, json=body, verify=verify, timeout=5, proxies=proxies,
        )
        try:
            payload = _payload_dict(resp.json())
        except ValueError:
            payload = None
        return HttpReply(status=resp.status_code, payload=payload)
    # см. комментарий в _requests_get_ex: кириллица в токене — ValueError, не сеть
    except (requests.exceptions.RequestException, UnicodeEncodeError) as exc:
        return HttpReply(
            status=None,
            payload=None,
            network_error=str(exc) or exc.__class__.__name__,
        )


def _urllib_get_ex(url: str, headers: dict, verify: bool) -> HttpReply:
    """Legacy urllib GET (без proxy). Оставлен для unit-тестов транспорта."""
    import socket
    import ssl
    import urllib.error
    import urllib.request
    ctx = None if verify else ssl._create_unverified_context()
    req = _request(url, headers)
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


def probe_fetch(completions_url: str, auth_header: Optional[str], *,
                verify: bool = True, config_proxy: Optional[str] = None,
                http_get_ex: Optional[Callable] = None) -> FetchOutcome:
    """models-цепочка (/config → /models) с кодами. 401 приоритетнее прочих кодов."""
    import time
    if http_get_ex is None:
        def get_ex(url, headers, verify):
            return _requests_get_ex(url, headers, verify, config_proxy=config_proxy)
    else:
        get_ex = http_get_ex
    base = api_base(completions_url)
    headers = _headers(auth_header)
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
               verify: bool = True, config_proxy: Optional[str] = None,
               http_post_ex: Optional[Callable] = None) -> tuple[Optional[int], str, int, Any]:
    """Минимальный POST в сам chat/completions (ступень 2 probe).
    → (status, net_err, latency_ms, payload); payload — разобранное JSON-тело или
    None, если ответ не JSON (HTML/текст 404-страницы гейтвея/Express).

    Отдаём тело, а не флаг «оно было JSON»: по нему проба отличает ответ на наш
    токен от ответа на заведомо мусорный, когда статус у обоих одинаковый."""
    import time
    if http_post_ex is None:
        def post_ex(url, headers, body, verify):
            return _requests_post_ex(url, headers, body, verify, config_proxy=config_proxy)
    else:
        post_ex = http_post_ex
    headers = _headers(auth_header)
    body = {"model": model, "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1, "stream": False}
    t0 = time.monotonic()
    reply = post_ex(completions_url, headers, body, verify)
    return (reply.status, reply.network_error, int((time.monotonic() - t0) * 1000),
            reply.payload)


def fetch_models(completions_url: str, auth_header: Optional[str], *,
                 verify: bool = True, config_proxy: Optional[str] = None,
                 http_get: Optional[Callable] = None):
    if http_get is None:
        def get(url, headers, verify):
            reply = _requests_get_ex(url, headers, verify, config_proxy=config_proxy)
            if reply.status is None:
                raise OSError(reply.network_error or "нет соединения")
            if reply.payload is None:
                raise ValueError(f"bad response status={reply.status}")
            return reply.payload
    else:
        get = http_get
    base = api_base(completions_url)
    if not base:
        return None
    headers = _headers(auth_header)
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
