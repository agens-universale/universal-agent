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

import json
from dataclasses import dataclass, replace
from typing import Optional

from gc_core import model_picker
from gc_core.utils.http_auth import authorization_attempts

# Заглушка для POST-проверки без заданной модели: инертное имя, которое сервер
# гарантированно не знает — ответ (401 vs 400/404/5xx) судит только токен.
_DUMMY_MODEL = "gc-connection-probe"

# Заведомо негодный, но синтаксически корректный токен. Если сервер отвечает на
# него тем же, что на пользовательский, — он их не различает, и «принят» заявлять
# нельзя (LiteLLM отдаёт 400 «No connected db.» на любой неизвестный ключ).
_CANARY_TOKEN = "Bearer gc-probe-invalid-token"
_CANARY_NAKED = "gc-probe-invalid-token"


def accepted_literal_auth(source: str | None, accepted: str | None) -> str | None:
    """Принятая форма для черновика; ссылки на секреты не раскрываются и не заменяются."""
    if not source or "${ENV:" in source or "${FILE:" in source:
        return None
    if accepted == source.strip() or accepted not in authorization_attempts(source):
        return None
    return accepted


def _non_latin1_reason(auth: Optional[str]) -> str:
    """Причина, по которой заголовок нельзя отправить, — или пустая строка.

    Заголовки HTTP кодируются в latin-1: токен с кириллицей рвёт запрос в клиенте,
    до сервера он не доходит вовсе. Молчать об этом нельзя — иначе вердикт
    сваливается в «сервер недоступен», хотя сервер ни при чём."""
    if not auth:
        return ""
    try:
        auth.encode("latin-1")
    except UnicodeEncodeError:
        return "в токене недопустимые символы — нужны только латиница и цифры"
    return ""


@dataclass(frozen=True)
class ProbeResult:
    verdict: str  # "ok" | "rejected" | "unknown"
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
    # True только когда до сервера не дошли (сетевой отказ). Отличает «сервер
    # лежит» от «не смогли проверить по своей причине» — рендер печатает разное,
    # и без флага клиентская ошибка читается как недоступный сервер.
    offline: bool = False
    # Слепок тела ответа — нужен ТОЛЬКО канарейке, чтобы понять, различает ли
    # сервер наш токен и заведомо мусорный. Одного http_status мало: LiteLLM на
    # оба отвечает 400, но валидному говорит «Invalid model name» (дошёл до
    # проверки модели), а мусорному «No connected db.» (застрял на поиске ключа).
    body_signature: str = ""
    # Форма Authorization, которую сервер принял (`authorization_attempts`).
    # None — токен не принимали / auth не интерпретировался. Только результат пробы;
    # исходный источник токена в форме и конфиге не меняется.
    accepted_auth: Optional[str] = None

    @property
    def reachable(self) -> bool:
        return self.verdict != "unknown"

    @property
    def auth_ok(self) -> bool:
        return self.http_status != 401


def probe(
    endpoint: str,
    auth: Optional[str],
    verify: bool | str,
    model: str,
    *,
    config_proxy: Optional[str] = None,
    fetch_ex=None,
    post_ex=None,
) -> ProbeResult:
    if fetch_ex is None:
        out = model_picker.probe_fetch(
            endpoint,
            auth,
            verify=verify,
            config_proxy=config_proxy,
        )
    else:
        out = fetch_ex(endpoint, auth, verify=verify)
    if out.models is not None:
        return ProbeResult(
            "ok", out.status, out.models, out.latency_ms, "доступен"
        )
    if out.status is None:
        return ProbeResult(
            "unknown",
            None,
            None,
            out.latency_ms,
            f"нет соединения ({out.network_error})",
            offline=True,
        )
    # Сервер жив, но models-цепочка не про него → ступень 2 (вердикт по реальному
    # маршруту). 401 цепочки — НЕ приговор токену: у gigacode /config собственная
    # plugin-авторизация (x-authorization-url, код 14011) → 401 при валидном Bearer,
    # а /models может отсутствовать (404); судим по самому chat/completions.
    # Без модели (первичная настройка: адрес+токен раньше шага модели) POST идёт
    # с инертной заглушкой и судит только токен.
    dummy = not model
    probe_model = model or _DUMMY_MODEL
    if post_ex is None:
        status, net_err, lat, payload = model_picker.probe_post(
            endpoint,
            auth,
            probe_model,
            verify=verify,
            config_proxy=config_proxy,
        )
    else:
        status, net_err, lat, payload = post_ex(
            endpoint, auth, probe_model, verify=verify
        )
    return _verdict_from_post(status, net_err, lat, payload, dummy)


def _body_signature(payload) -> str:
    """Стабильный слепок тела ответа для сравнения канарейкой. Сортируем ключи,
    чтобы порядок в JSON не выдавал различие там, где его нет; не-JSON и пустое
    тело дают пустую строку — сравнение тогда сводится к одному статусу."""
    if not isinstance(payload, dict):
        return ""
    try:
        return json.dumps(payload, sort_keys=True, ensure_ascii=False)[:400]
    except (TypeError, ValueError):
        return ""


def _verdict_from_post(
    status, net_err, lat, payload, dummy: bool
) -> ProbeResult:
    """Ответ chat/completions в вердикт. Общая для probe() и probe_auth(): правила
    чтения одного и того же маршрута не должны разъезжаться между вызывающими.

    `payload` — разобранное JSON-тело или None. Тело нужно и для вердикта (404 с
    JSON = ответило приложение), и для канарейки: она сравнивает слепок."""
    json_body = payload is not None
    sig = _body_signature(payload)
    if status is None:
        return ProbeResult(
            "unknown",
            None,
            None,
            lat,
            f"нет соединения ({net_err})",
            offline=True,
        )
    if 200 <= status < 300:
        return ProbeResult(
            "ok", status, None, lat, "доступен", body_signature=sig
        )
    if status == 401:
        return ProbeResult(
            "rejected",
            401,
            None,
            lat,
            "токен отклонён (401)",
            body_signature=sig,
        )
    if status == 404:
        if not json_body:
            # 404 без JSON неоднозначен: HTML «Cannot POST» пути ИЛИ роутер, не знающий
            # модель (gigacode ST на несуществующую модель отвечает так же) — не гадаем.
            if (
                dummy
            ):  # модель-заглушка наша — совсем не вердикт о данных юзера
                return ProbeResult(
                    "unknown",
                    404,
                    None,
                    lat,
                    "не удалось проверить (404: путь или модель)",
                    body_signature=sig,
                )
            return ProbeResult(
                "rejected",
                404,
                None,
                lat,
                "отклонено сервером (404): проверьте адрес и имя модели",
                body_signature=sig,
            )
        if (
            dummy
        ):  # JSON-ошибка на заглушку — приложение ответило, auth пройден
            return ProbeResult(
                "ok", status, None, lat, "доступен", body_signature=sig
            )
        return ProbeResult(
            "rejected",
            404,
            None,
            lat,
            "отклонено сервером (404): проверьте имя модели",
            body_signature=sig,
        )
    if dummy:
        # 400/422/5xx на заглушку: до «плохая модель/тело» нельзя дойти,
        # не пройдя авторизацию → сервер жив, токен принят.
        return ProbeResult(
            "ok", status, None, lat, "доступен", body_signature=sig
        )
    return ProbeResult(
        "unknown",
        status,
        None,
        lat,
        f"сервер ответил {status}",
        body_signature=sig,
    )


def probe_auth(
    endpoint: str,
    auth: Optional[str],
    verify: bool | str,
    model: str,
    *,
    config_proxy: Optional[str] = None,
    post_ex=None,
) -> ProbeResult:
    """Вердикт об авторизации СТРОГО по chat/completions.

    models-цепочка сюда не входит намеренно: у OpenRouter `/api/v1/models` публичен
    и отвечает без токена — судя по нему, проба писала бы «авторизация не требуется»
    ровно про тот маршрут, который токен как раз требует."""
    reason = _non_latin1_reason(auth)
    if reason:
        return ProbeResult(
            "rejected", None, None, 0, reason, auth_state="rejected"
        )
    dummy = not model
    probe_model = model or _DUMMY_MODEL
    if post_ex is None:
        status, net_err, lat, payload = model_picker.probe_post(
            endpoint,
            auth,
            probe_model,
            verify=verify,
            config_proxy=config_proxy,
        )
    else:
        status, net_err, lat, payload = post_ex(
            endpoint, auth, probe_model, verify=verify
        )
    return _verdict_from_post(status, net_err, lat, payload, dummy)


def interpret_auth(
    control: ProbeResult,
    with_token: Optional[ProbeResult],
    has_token: bool,
    canary: Optional[ProbeResult] = None,
) -> ProbeResult:
    """Смысл авторизации из differential-probe. control — запрос БЕЗ токена;
    with_token — повтор С токеном (или None, если не слали, т.к. незачем).
    ok → авторизация не требуется ЛИБО токен реально принят; rejected → сервер
    требует авторизацию / токен отклонён; unknown → сеть/VPN (не наказываем).

    Ключевая идея: если сервер отдаёт ответ БЕЗ токена — токен там ни на что не
    влияет (любой «пройдёт»), и честно писать «не требует», а не «токен принят»."""
    if control.verdict == "unknown":
        return control
    if control.verdict == "ok":
        detail = (
            "сервер не требует токен — введённый токен не проверяется"
            if has_token
            else "доступ без токена — авторизация не требуется"
        )
        return ProbeResult(
            "ok",
            control.http_status,
            control.models,
            control.latency_ms,
            detail,
            auth_state="not_required",
        )
    # control rejected: авторизация значима только на 401 (404/иное — путь/модель)
    if control.http_status == 401:
        if not has_token:
            return ProbeResult(
                "rejected",
                401,
                None,
                control.latency_ms,
                "сервер требует авторизацию (401)",
                auth_state="required",
            )
        if with_token is None:
            return control
        # Канарейка старше всех правил ниже. Оба они выводят принятие из того, что
        # отказ «переехал» дальше авторизации: ступень 2 — на модели-заглушке
        # (400/5xx = «до валидации тела не дойти, не пройдя auth»), сдвиг 401→не-401 —
        # на смене кода. Посылка одна, и ломается она одинаково: LiteLLM отвечает 400
        # на ЛЮБОЙ негодный ключ.
        #
        # Сравниваем статус И тело: на том же LiteLLM валидный токен и мусорный оба
        # дают 400, но валидному сервер говорит «Invalid model name» (дошёл до
        # проверки модели), а мусорному «No connected db.» (застрял на поиске ключа
        # в базе). По одному статусу сервер выглядел бы неразличающим, и настоящий
        # токен объявлялся бы неподтверждённым — живой смок 2026-08-17.
        if (
            canary is not None
            and canary.http_status == with_token.http_status
            and canary.body_signature == with_token.body_signature
        ):
            # `replace`, а не пересборка по полям: `offline` (и любое будущее
            # поле ProbeResult) обязан доехать до потребителей — ручной
            # перечень терял его молча, auth_state здесь намеренно пуст.
            return replace(
                with_token,
                verdict="unknown",
                models=None,
                detail=f"не удалось подтвердить токен: на заведомо неверный "
                f"сервер отвечает так же ({with_token.http_status})",
                auth_state=None,
            )
        if with_token.verdict == "ok":
            return replace(
                with_token,
                verdict="ok",
                detail="токен принят",
                auth_state="accepted",
            )
        if with_token.http_status not in (None, 401):
            # Отказ «переехал» дальше авторизации (401 без токена → 404/5xx с токеном
            # на ТОМ ЖЕ маршруте) → путь существует и токен принят; остаток отказа —
            # про модель/сервер, забота других секций (живой кейс gigacode ST:
            # несуществующая модель даёт 404-не-JSON, неотличимый от кривого пути).
            return replace(
                with_token,
                verdict="ok",
                detail="токен принят",
                auth_state="accepted",
            )
        auth_state = "rejected" if with_token.http_status == 401 else None
        return replace(with_token, auth_state=auth_state)
    return control


def _canary_for(auth: str) -> str:
    """Канарейка в той же форме, что и проверяемый заголовок.

    Сервер, который отличает «голый ключ» от ``Bearer``, на чужой схеме
    ответит одинаково и на рабочий токен, и на мусор — сравнение было бы
    не про этот токен."""
    if auth.lower().startswith("bearer "):
        return _CANARY_TOKEN
    return _CANARY_NAKED


def probe_connection(
    endpoint: str,
    token: Optional[str],
    verify: bool | str,
    model: str,
    *,
    config_proxy: Optional[str] = None,
    probe_fn=None,
) -> ProbeResult:
    """Полный вердикт «сервер + токен» для строки «Подключение».

    Контроль БЕЗ токена отвечает на вопрос «нужна ли тут авторизация вообще»;
    повтор С токеном — «принимают ли именно этот». Канарейка добирается только в
    спорную ветку: сервер отказал не 401-м, и без неё пришлось бы гадать, отказ
    это про токен или уже про модель.

    Принятая форма заголовка возвращается вызывающему для рабочего запроса.

    Живёт здесь, а не в экранах: пробу зовут и /settings, и мастер, а скопированный
    differential-танец разъехался бы при первой правке одной из копий."""
    run = probe_fn or probe_auth
    attempts = authorization_attempts(token)
    has_token = bool(attempts)
    control = run(endpoint, None, verify, model, config_proxy=config_proxy)
    with_token = canary = None
    tried_variant: Optional[str] = None
    if (
        control.verdict == "rejected"
        and control.http_status == 401
        and has_token
    ):
        for index, variant in enumerate(attempts):
            with_token = run(
                endpoint, variant, verify, model, config_proxy=config_proxy
            )
            tried_variant = variant
            status = with_token.http_status
            if status == 401 and index + 1 < len(attempts):
                continue
            ambiguous = (
                status is not None
                and status != 401
                and not (200 <= status < 300)
            )
            if ambiguous:
                canary = run(
                    endpoint,
                    _canary_for(variant),
                    verify,
                    model,
                    config_proxy=config_proxy,
                )
            break
    result = interpret_auth(control, with_token, has_token, canary=canary)
    if result.auth_state == "accepted" and tried_variant is not None:
        return replace(result, accepted_auth=tried_variant)
    return result
