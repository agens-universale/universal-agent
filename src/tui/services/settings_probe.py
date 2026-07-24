"""Probe соединения для /settings (спека B2, контракт §3.1).

Три вердикта (балансная модель): ok — сервер подтвердил; rejected — сервер ОТВЕТИЛ
отказом (достоверно: 401 токен, 404 путь/модель); unknown — не достучались либо
не-вердиктный ответ (5xx и пр.) — данные НЕ виноваты, возможно VPN/туннель.
Двухступенчато: models-цепочка → при 404-подобном фолбэк POST в сам endpoint.
Сервис чистый: сеть только через инъецируемые model_picker.probe_fetch/probe_post."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import model_picker


@dataclass(frozen=True)
class ProbeResult:
    verdict: str                 # "ok" | "rejected" | "unknown"
    http_status: Optional[int]
    models: Optional[list]
    latency_ms: int
    detail: str
    # Смысл авторизации (ставит interpret_auth; None — auth не интерпретировался,
    # напр. сырой config-probe): "accepted" — токен реально принят;
    # "not_required" — сервер отдаёт без токена; "required" — сервер требует
    # авторизацию, токена не было; "rejected" — токен отклонён (401).
    # detail — только текст для человека, вердикт по нему не восстанавливать.
    auth_state: Optional[str] = None

    @property
    def reachable(self) -> bool:
        return self.verdict != "unknown"

    @property
    def auth_ok(self) -> bool:
        return self.http_status != 401


def probe(endpoint: str, auth: Optional[str], verify: bool, model: str, *,
          config_proxy: Optional[str] = None, fetch_ex=None, post_ex=None) -> ProbeResult:
    if fetch_ex is None:
        out = model_picker.probe_fetch(
            endpoint, auth, verify=verify, config_proxy=config_proxy,
        )
    else:
        out = fetch_ex(endpoint, auth, verify=verify)
    if out.models:
        return ProbeResult("ok", out.status, out.models, out.latency_ms, "доступен")
    if out.status == 401:
        return ProbeResult("rejected", 401, None, out.latency_ms, "токен отклонён (401)")
    if out.status is None:
        return ProbeResult("unknown", None, None, out.latency_ms,
                           f"нет соединения ({out.network_error})")
    # Сервер жив, но models-цепочка не про него (404/403/5xx на /config|/models) → ступень 2.
    if not model:
        return ProbeResult("unknown", out.status, None, out.latency_ms,
                           "путь не проверен — задайте модель")
    if post_ex is None:
        status, net_err, lat = model_picker.probe_post(
            endpoint, auth, model, verify=verify, config_proxy=config_proxy,
        )
    else:
        status, net_err, lat = post_ex(endpoint, auth, model, verify=verify)
    if status is not None and 200 <= status < 300:
        return ProbeResult("ok", status, None, lat, "доступен")
    if status == 401:
        return ProbeResult("rejected", 401, None, lat, "токен отклонён (401)")
    if status == 404:
        return ProbeResult("rejected", 404, None, lat,
                           "отклонено сервером (404): проверьте путь и имя модели")
    if status is None:
        return ProbeResult("unknown", None, None, lat, f"нет соединения ({net_err})")
    return ProbeResult("unknown", status, None, lat, f"сервер ответил {status}")


def interpret_auth(control: ProbeResult, with_token: Optional[ProbeResult],
                   has_token: bool) -> ProbeResult:
    """Смысл авторизации из differential-probe. control — запрос БЕЗ токена;
    with_token — повтор С токеном (или None, если не слали, т.к. незачем).
    ok → авторизация не требуется ЛИБО токен реально принят; rejected → сервер
    требует авторизацию / токен отклонён; unknown → сеть/VPN (не наказываем).

    Ключевая идея: если сервер отдаёт ответ БЕЗ токена — токен там ни на что не
    влияет (любой «пройдёт»), и честно писать «не требует», а не «токен принят»."""
    if control.verdict == "unknown":
        return control
    if control.verdict == "ok":
        detail = ("сервер не требует токен — введённый токен не проверяется"
                  if has_token else "доступ без токена — авторизация не требуется")
        return ProbeResult("ok", control.http_status, control.models,
                           control.latency_ms, detail, auth_state="not_required")
    # control rejected: авторизация значима только на 401 (404/иное — путь/модель)
    if control.http_status == 401:
        if not has_token:
            return ProbeResult("rejected", 401, None, control.latency_ms,
                               "сервер требует авторизацию (401)", auth_state="required")
        if with_token is None:
            return control
        if with_token.verdict == "ok":
            return ProbeResult("ok", with_token.http_status, with_token.models,
                               with_token.latency_ms, "токен принят",
                               auth_state="accepted")
        auth_state = "rejected" if with_token.http_status == 401 else None
        return ProbeResult(with_token.verdict, with_token.http_status,
                           with_token.models, with_token.latency_ms,
                           with_token.detail, auth_state=auth_state)
    return control
