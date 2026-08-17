"""Probe соединения для /settings (спека B2, контракт §3.1).

Три вердикта (балансная модель): ok — сервер подтвердил; rejected — сервер ОТВЕТИЛ
отказом (достоверно: 401 токен, 404 путь/модель); unknown — не достучались либо
не-вердиктный ответ (5xx и пр.) — данные НЕ виноваты, возможно VPN/туннель.
Двухступенчато: models-цепочка → при любом не-сетевом отказе (включая 401 —
у /config|/models может быть своя авторизация/их может не быть) фолбэк POST в сам
endpoint; вердикт о токене — по реальному маршруту. Без модели POST шлётся с
инертной заглушкой и судит ТОЛЬКО токен: не-401 (400/422/5xx/404-с-JSON) значит
«сервер пропустил авторизацию до валидации тела» → токен принят. 404 различаем
по телу: JSON — ответило приложение (модель/тело), не-JSON (HTML «Cannot POST»
Express/гейтвея) — путь не найден, токен не виним.
Сервис чистый: сеть только через инъецируемые model_picker.probe_fetch/probe_post."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from gc_core import model_picker

# Заглушка для POST-проверки без заданной модели: инертное имя, которое сервер
# гарантированно не знает — ответ (401 vs 400/404/5xx) судит только токен.
_DUMMY_MODEL = "gc-connection-probe"


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
    if out.status is None:
        return ProbeResult("unknown", None, None, out.latency_ms,
                           f"нет соединения ({out.network_error})")
    # Сервер жив, но models-цепочка не про него → ступень 2 (вердикт по реальному
    # маршруту). 401 цепочки — НЕ приговор токену: у gigacode /config собственная
    # plugin-авторизация (x-authorization-url, код 14011) → 401 при валидном Bearer,
    # а /models может отсутствовать (404); судим по самому chat/completions.
    # Без модели (первичная настройка: адрес+токен раньше шага модели) POST идёт
    # с инертной заглушкой и судит только токен.
    dummy = not model
    probe_model = model or _DUMMY_MODEL
    if post_ex is None:
        status, net_err, lat, json_body = model_picker.probe_post(
            endpoint, auth, probe_model, verify=verify, config_proxy=config_proxy,
        )
    else:
        status, net_err, lat, json_body = post_ex(endpoint, auth, probe_model, verify=verify)
    if status is None:
        return ProbeResult("unknown", None, None, lat, f"нет соединения ({net_err})")
    if 200 <= status < 300:
        return ProbeResult("ok", status, None, lat, "доступен")
    if status == 401:
        return ProbeResult("rejected", 401, None, lat, "токен отклонён (401)")
    if status == 404:
        if not json_body:
            # 404 без JSON неоднозначен: HTML «Cannot POST» пути ИЛИ роутер, не знающий
            # модель (gigacode ST на несуществующую модель отвечает так же) — не гадаем.
            if dummy:       # модель-заглушка наша — совсем не вердикт о данных юзера
                return ProbeResult("unknown", 404, None, lat,
                                   "не удалось проверить (404: путь или модель)")
            return ProbeResult("rejected", 404, None, lat,
                               "отклонено сервером (404): проверьте адрес и имя модели")
        if dummy:           # JSON-ошибка на заглушку — приложение ответило, auth пройден
            return ProbeResult("ok", status, None, lat, "доступен")
        return ProbeResult("rejected", 404, None, lat,
                           "отклонено сервером (404): проверьте имя модели")
    if dummy:
        # 400/422/5xx на заглушку: до «плохая модель/тело» нельзя дойти,
        # не пройдя авторизацию → сервер жив, токен принят.
        return ProbeResult("ok", status, None, lat, "доступен")
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
        if with_token.http_status not in (None, 401):
            # Отказ «переехал» дальше авторизации (401 без токена → 404/5xx с токеном
            # на ТОМ ЖЕ маршруте) → путь существует, токен принят; остаток отказа —
            # про модель/сервер, это забота других секций (живой кейс gigacode ST:
            # несуществующая модель даёт 404-не-JSON, неотличимый от кривого пути).
            return ProbeResult("ok", with_token.http_status, with_token.models,
                               with_token.latency_ms, "токен принят",
                               auth_state="accepted")
        auth_state = "rejected" if with_token.http_status == 401 else None
        return ProbeResult(with_token.verdict, with_token.http_status,
                           with_token.models, with_token.latency_ms,
                           with_token.detail, auth_state=auth_state)
    return control
