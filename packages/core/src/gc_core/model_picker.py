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

from gc_core.utils.http_auth import authorization_attempts

# Пути каталога моделей: /config (gigacode) предпочтительнее /models.
_CONFIG_PATH = "/config"
_MODELS_PATH = "/models"
_MODEL_ID_KEYS = ("id", "name", "model")


@dataclass(frozen=True)
class ModelInfo:
    id: str
    description: str = ""
    tool_capable: Optional[bool] = None  # None = неизвестно (нет ⚠)
    context: Optional[int] = None


@dataclass(frozen=True)
class HttpReply:
    """Ответ одного HTTP-вызова probe-путей: код ЛИБО сетевая ошибка."""

    status: Optional[int]  # None = до сервера не достучались
    payload: Optional[dict]
    network_error: str = ""


@dataclass(frozen=True)
class FetchOutcome:
    models: Optional[list]
    status: Optional[int]
    network_error: str
    latency_ms: int
    # Сырое тело ответа, из которого собран список id (форма показывает JSON карточки).
    payload: Optional[dict] = None


def match_model_objects(payload: Optional[dict], model_id: str) -> list[dict]:
    """Объекты карточки с ``id``/``name``/``model`` == model_id из сырого тела.

    Ищет в ``data[]``, верхнем ``models[]`` и ``chat.models[]`` — что есть в теле.
    Совпало несколько — все, по порядку обхода коллекций и элементов.
    """
    if not isinstance(payload, dict) or not model_id:
        return []
    found: list[dict] = []
    for collection in _model_object_collections(payload):
        for obj in collection:
            if _card_matches_model_id(obj, model_id):
                found.append(obj)
    return found


def _card_matches_model_id(obj: Any, model_id: str) -> bool:
    if not isinstance(obj, dict):
        return False
    for key in _MODEL_ID_KEYS:
        if key in obj and str(obj[key]) == model_id:
            return True
    return False


def _model_object_collections(payload: dict) -> list[list]:
    collections: list[list] = []
    data = payload.get("data")
    if isinstance(data, list):
        collections.append(data)
    models = payload.get("models")
    if isinstance(models, list):
        collections.append(models)
    chat = payload.get("chat")
    if isinstance(chat, dict):
        chat_models = chat.get("models")
        if isinstance(chat_models, list):
            collections.append(chat_models)
    return collections


def parse_config_models(payload: dict) -> list:
    models = (((payload or {}).get("chat") or {}).get("models")) or []
    out: list = []
    for m in models:
        if not isinstance(m, dict) or not m.get("model"):
            continue
        ps = m.get("plugin_side") or {}
        tc = ps.get("supports_parallel_tool_calls")
        out.append(
            ModelInfo(
                id=str(m["model"]),
                description=str(m.get("description") or ""),
                tool_capable=tc if isinstance(tc, bool) else None,
                context=ps.get("context_window_size")
                if isinstance(ps.get("context_window_size"), int)
                else None,
            )
        )
    return out


def parse_models_list(payload: dict) -> list:
    data = (payload or {}).get("data") or []
    out: list = []
    for m in data:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        eps = m.get("endpoints")
        if isinstance(eps, list) and "/chat/completions" not in eps:
            continue  # есть список эндпоинтов и chat/completions нет → пропуск
        out.append(ModelInfo(id=str(m["id"])))
    return out


def api_base(completions_url: str) -> str:
    """Корень API по адресу конкретного метода. Аудио-методы (STT-endpoint голосового
    ввода — тот же OpenAI-совместимый `/v1`) режутся так же, как chat: без них
    `fetch_models` стучался бы в `…/audio/transcriptions/models` — мёртвый путь."""
    u = (completions_url or "").rstrip("/")
    for suffix in (
        "/chat/completions",
        "/completions",
        "/audio/transcriptions",
        "/audio/translations",
    ):
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
        url,
        data=data,
        method=method,
        headers={"User-Agent": _user_agent(), **headers},
    )


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
    verify: bool | str,
    *,
    config_proxy: Optional[str] = None,
) -> HttpReply:
    """GET через backend-сессию: trust_env=False + resolve_proxies."""
    import httpx

    from gc_core.utils.http_proxy import resolve_proxies
    from gc_core.utils.http_session import NO_NETRC_SESSION

    proxies = resolve_proxies(config_proxy, os.environ, url)
    try:
        resp = NO_NETRC_SESSION.get(
            url,
            headers=headers,
            verify=verify,  # NOSONAR: use_ssl:false из config.yml — осознанный opt-in, см. CONTRIBUTING.md
            timeout=5,
            proxies=proxies,
        )
        try:
            payload = _payload_dict(resp.json())
        except ValueError:
            payload = None
        return HttpReply(status=resp.status_code, payload=payload)
    # UnicodeEncodeError — не сеть: заголовки кодируются в latin-1, и токен с
    # кириллицей рвёт запрос в клиенте. Это ValueError, мимо RequestException, —
    # без перехвата исключение уходит наверх и там читается как «сервер недоступен».
    except (httpx.HTTPError, UnicodeEncodeError) as exc:
        return HttpReply(
            status=None,
            payload=None,
            network_error=str(exc) or exc.__class__.__name__,
        )


def _requests_post_ex(
    url: str,
    headers: dict,
    body: dict,
    verify: bool | str,
    *,
    config_proxy: Optional[str] = None,
) -> HttpReply:
    """POST через backend-сессию: trust_env=False + resolve_proxies."""
    import httpx

    from gc_core.utils.http_proxy import resolve_proxies
    from gc_core.utils.http_session import NO_NETRC_SESSION

    proxies = resolve_proxies(config_proxy, os.environ, url)
    try:
        resp = NO_NETRC_SESSION.post(
            url,
            headers=headers,
            json=body,
            verify=verify,  # NOSONAR: use_ssl:false из config.yml — осознанный opt-in, см. CONTRIBUTING.md
            timeout=5,
            proxies=proxies,
        )
        try:
            payload = _payload_dict(resp.json())
        except ValueError:
            payload = None
        return HttpReply(status=resp.status_code, payload=payload)
    # см. комментарий в _requests_get_ex: кириллица в токене — ValueError, не сеть
    except (httpx.HTTPError, UnicodeEncodeError) as exc:
        return HttpReply(
            status=None,
            payload=None,
            network_error=str(exc) or exc.__class__.__name__,
        )


def _urllib_get_ex(url: str, headers: dict, verify: bool | str) -> HttpReply:
    """Legacy urllib GET (без proxy). Оставлен для unit-тестов транспорта."""
    import urllib.error
    import urllib.request

    from gc_core.utils.async_http import build_ssl_context

    ctx = build_ssl_context(verify)
    req = _request(url, headers)
    try:
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            try:
                payload = json.loads(resp.read().decode("utf-8"))
            except ValueError:
                payload = None
            return HttpReply(status=resp.status, payload=payload)
    except urllib.error.HTTPError as exc:
        return HttpReply(status=exc.code, payload=None)
    except OSError as exc:
        return HttpReply(
            status=None,
            payload=None,
            network_error=str(exc) or exc.__class__.__name__,
        )


def _parse_reply_models(reply: HttpReply, parser: Callable) -> list:
    """Список моделей из ответа с кодом и телом; пусто, если ответа нет или парсер упал."""
    if reply.status is None or reply.payload is None:
        return []
    try:
        return parser(reply.payload)
    except Exception:
        return []


def probe_fetch(
    completions_url: str,
    auth_header: Optional[str],
    *,
    verify: bool | str = True,
    config_proxy: Optional[str] = None,
    http_get_ex: Optional[Callable] = None,
) -> FetchOutcome:
    """models-цепочка (/config → /models); без доступного каталога 401 приоритетнее прочих кодов."""
    import time

    get_ex = http_get_ex or (
        lambda url, headers, verify: _requests_get_ex(
            url, headers, verify, config_proxy=config_proxy
        )
    )
    base = api_base(completions_url)
    if not base:
        return FetchOutcome(None, None, "пустой адрес", 0)
    auth_forms = authorization_attempts(auth_header) or [auth_header]
    t0 = time.monotonic()
    statuses: list = []
    net_errors: list = []
    empty_catalog_status = None
    for path, parser in (
        (_CONFIG_PATH, parse_config_models),
        (_MODELS_PATH, parse_models_list),
    ):
        reply = _probe_auth_forms(get_ex, base + path, auth_forms, verify)
        hit = _catalog_hit(reply, parser, path)
        if hit is not None:
            models, payload = hit
            if models:
                return FetchOutcome(
                    models,
                    reply.status,
                    "",
                    int((time.monotonic() - t0) * 1000),
                    payload=payload,
                )
            # Пустой /config ещё не исключает каталог на /models.
            empty_catalog_status = reply.status
        elif reply.status is not None:
            statuses.append(reply.status)
        else:
            net_errors.append(reply.network_error)
    return _probe_fetch_fallback(
        empty_catalog_status,
        statuses,
        net_errors,
        int((time.monotonic() - t0) * 1000),
    )


def _probe_auth_forms(
    get_ex: Callable, url: str, auth_forms: list, verify: bool | str
) -> HttpReply:
    """Один путь: перебрать формы Authorization, остановиться после не-401."""
    reply = HttpReply(None, None, "нет попыток")
    for index, form in enumerate(auth_forms):
        reply = get_ex(url, _headers(form), verify)
        if reply.status != 401 or index + 1 == len(auth_forms):
            break
    return reply


def _catalog_hit(
    reply: HttpReply, parser: Callable, path: str
) -> tuple[list, Optional[dict]] | None:
    """Успешный каталог (200 + разобранный список или пустой raw-массив)."""
    if reply.status is None or not (200 <= reply.status < 300):
        return None
    parsed = _parse_reply_models(reply, parser)
    payload = reply.payload if isinstance(reply.payload, dict) else {}
    raw_models = _raw_models_collection(payload, path)
    if not (parsed or isinstance(raw_models, list)):
        return None
    return parsed, payload if isinstance(payload, dict) else None


def _raw_models_collection(payload: dict, path: str) -> Any:
    source = payload.get("chat") if path == _CONFIG_PATH else payload
    if not isinstance(source, dict):
        return None
    return source.get("models" if path == _CONFIG_PATH else "data")


def _probe_fetch_fallback(
    empty_catalog_status: Optional[int],
    statuses: list,
    net_errors: list,
    latency: int,
) -> FetchOutcome:
    if empty_catalog_status is not None:
        return FetchOutcome([], empty_catalog_status, "", latency)
    if 401 in statuses:
        return FetchOutcome(None, 401, "", latency)
    if statuses:
        return FetchOutcome(None, statuses[-1], "", latency)
    return FetchOutcome(
        None,
        None,
        "; ".join(dict.fromkeys(net_errors)) or "нет соединения",
        latency,
    )


def fetch_models_catalog(
    completions_url: str,
    auth_header: Optional[str],
    *,
    verify: bool | str = True,
    config_proxy: Optional[str] = None,
    http_get_ex: Optional[Callable] = None,
) -> tuple[Optional[list], Optional[dict]]:
    """Список id и сырое тело ответа — для формы модели (карточка JSON)."""
    out = probe_fetch(
        completions_url,
        auth_header,
        verify=verify,
        config_proxy=config_proxy,
        http_get_ex=http_get_ex,
    )
    return out.models, out.payload


def probe_post(
    completions_url: str,
    auth_header: Optional[str],
    model: str,
    *,
    verify: bool | str = True,
    config_proxy: Optional[str] = None,
    http_post_ex: Optional[Callable] = None,
) -> tuple[Optional[int], str, int, Any]:
    """Минимальный POST в сам chat/completions (ступень 2 probe).
    → (status, net_err, latency_ms, payload); payload — разобранное JSON-тело или
    None, если ответ не JSON (HTML/текст 404-страницы гейтвея/Express).

    Отдаём тело, а не флаг «оно было JSON»: по нему проба отличает ответ на наш
    токен от ответа на заведомо мусорный, когда статус у обоих одинаковый."""
    import time

    if http_post_ex is None:

        def post_ex(url, headers, body, verify):
            return _requests_post_ex(
                url, headers, body, verify, config_proxy=config_proxy
            )
    else:
        post_ex = http_post_ex
    headers = _headers(auth_header)
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    t0 = time.monotonic()
    reply = post_ex(completions_url, headers, body, verify)
    return (
        reply.status,
        reply.network_error,
        int((time.monotonic() - t0) * 1000),
        reply.payload,
    )


def fetch_models(
    completions_url: str,
    auth_header: Optional[str],
    *,
    verify: bool | str = True,
    config_proxy: Optional[str] = None,
    http_get: Optional[Callable] = None,
):
    if http_get is None:
        return probe_fetch(
            completions_url,
            auth_header,
            verify=verify,
            config_proxy=config_proxy,
        ).models
    else:
        get = http_get
    base = api_base(completions_url)
    if not base:
        return None
    headers = _headers(auth_header)
    try:
        cfg = get(base + _CONFIG_PATH, headers, verify)
        parsed = parse_config_models(cfg)
        if parsed:
            return parsed
    except Exception:
        pass
    try:
        lst = get(base + _MODELS_PATH, headers, verify)
        return parse_models_list(lst)
    except Exception:
        pass
    return None
