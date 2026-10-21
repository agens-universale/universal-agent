"""An isolated visual challenge, never containing user data."""

import base64
import io
import logging
import secrets
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from types import MappingProxyType

import httpx

from gc_core.utils.backend_errors import (
    EmbeddedChoiceError,
    http_status_code,
    is_image_rejection_error,
)
from gc_core.utils.http_auth import (
    remember_accepted_authorization,
    server_authorization_attempts,
)
from gc_core.utils.llm_chat_client import (
    ChatCompletionText,
    PreparedChatCompletion,
    arequest_chat_completion_once,
)


class ProbeStatus(StrEnum):
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    UNSUPPORTED = "unsupported"
    TRANSIENT = "transient"
    BLOCKED = "blocked"
    INSUFFICIENT_BUDGET = "insufficient_budget"


@dataclass(frozen=True)
class ProbeResult:
    status: ProbeStatus
    reason: str
    retry_after_sec: float = 0.0


@dataclass(frozen=True)
class ProbeImage:
    code: str = field(repr=False)
    png: bytes = field(repr=False)


def make_challenge(
    *, retry: bool, previous_code: str | None = None
) -> ProbeImage:
    from PIL import Image, ImageDraw, ImageFont

    code = previous_code
    while code is None or code == previous_code:
        code = "".join(
            secrets.choice("2345679ACDEFGHJKMNPRUVWXY") for _ in range(6)
        )
    size, font_size = ((384, 192), 64) if retry else ((320, 120), 48)
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=font_size)
    left, top, right, bottom = draw.textbbox((0, 0), code, font=font)
    position = (
        (size[0] - (right - left)) / 2 - left,
        (size[1] - (bottom - top)) / 2 - top,
    )
    draw.text(position, code, font=font, fill=0)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return ProbeImage(code, output.getvalue())


def classify_answer(code: str, outcome: ChatCompletionText) -> ProbeResult:
    if outcome.text.strip() == code:
        return ProbeResult(ProbeStatus.CONFIRMED, "code_read")
    if (outcome.finish_reason or "").lower() in {"length", "max_tokens"}:
        return ProbeResult(ProbeStatus.INSUFFICIENT_BUDGET, "output_limit")
    return ProbeResult(ProbeStatus.UNCONFIRMED, "code_mismatch")


def _retry_after(exc: BaseException) -> float:
    if not isinstance(exc, httpx.HTTPStatusError):
        return 30.0
    value = exc.response.headers.get("Retry-After", "")
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            seconds = (date - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            seconds = 30.0
    return max(30.0, seconds)


def _classify_error(exc: Exception) -> ProbeResult:
    if is_image_rejection_error(exc):
        return ProbeResult(ProbeStatus.UNSUPPORTED, "image_unsupported")
    status = http_status_code(exc)
    if isinstance(exc, EmbeddedChoiceError):
        status = exc.status_code
    if (
        isinstance(exc, httpx.TransportError)
        or status == 429
        or (status is not None and status >= 500)
    ):
        return ProbeResult(ProbeStatus.TRANSIENT, "network", _retry_after(exc))
    if status == 413:
        return ProbeResult(ProbeStatus.BLOCKED, "probe_payload_rejected")
    if status in {401, 403}:
        return ProbeResult(ProbeStatus.BLOCKED, "access_denied")
    return ProbeResult(ProbeStatus.BLOCKED, "route_configuration")


def _record_result(ctx, result, attempts, started, finish_reason=None):
    (getattr(ctx, "logger", None) or logging.getLogger(__name__)).info(
        "vision_probe status=%s reason=%s attempts=%s",
        result.status.value,
        result.reason,
        attempts,
        extra={
            "event": "vision_probe",
            "status": result.status.value,
            "reason": result.reason,
            "attempts": attempts,
            "duration_ms": (time.perf_counter() - started) * 1000,
            "finish_reason": finish_reason,
        },
    )
    return result


def _authorization_forms(
    prepared: PreparedChatCompletion,
) -> tuple[str | None, list]:
    """Заголовок авторизации и его формы: выученная транспортом — первой."""
    auth_key = next(
        (key for key in prepared.headers if key.lower() == "authorization"),
        None,
    )
    raw = prepared.headers.get(auth_key) if auth_key else None
    return auth_key, server_authorization_attempts(
        prepared.url, prepared.resolved.model, raw
    )


def _alternative_auth_applies(exc: Exception, auth_forms: list) -> bool:
    """401 при двух формах авторизации — стоит повторить со второй."""
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == 401
        and len(auth_forms) == 2
    )


def _with_auth(
    prepared: PreparedChatCompletion, auth_key: str | None, form: str
) -> PreparedChatCompletion:
    """Тот же запрос с заданной формой авторизации."""
    headers = dict(prepared.headers)
    headers[auth_key] = form
    return replace(prepared, headers=MappingProxyType(headers))


def _remember_auth(
    configured: PreparedChatCompletion,
    sent: PreparedChatCompletion,
    auth_key: str | None,
) -> None:
    """Форма, с которой проба прошла, — в общую память транспорта."""
    if auth_key is not None:
        remember_accepted_authorization(
            configured.url,
            configured.resolved.model,
            configured.headers[auth_key],
            sent.headers[auth_key],
        )


async def aprobe_route(
    ctx, state, prepared: PreparedChatCompletion
) -> ProbeResult:
    started = time.perf_counter()
    previous = None
    configured = prepared
    auth_key, auth_forms = _authorization_forms(prepared)
    if auth_forms:
        prepared = _with_auth(prepared, auth_key, auth_forms[0])
    authorization_alternative = False
    requests = 0
    for attempt in range(2):
        challenge = make_challenge(retry=bool(attempt), previous_code=previous)
        url = (
            "data:image/png;base64," + base64.b64encode(challenge.png).decode()
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Read the code in the image. Reply with the code only.",
                    },
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ]
        while True:
            requests += 1
            try:
                outcome = await arequest_chat_completion_once(
                    ctx,
                    state,
                    prepared=prepared,
                    messages=messages,
                    max_tokens=512,
                    request_label="vision_probe",
                )
                break
            except (httpx.HTTPError, EmbeddedChoiceError, ValueError) as exc:
                if not authorization_alternative and _alternative_auth_applies(
                    exc, auth_forms
                ):
                    prepared = _with_auth(prepared, auth_key, auth_forms[1])
                    authorization_alternative = True
                    continue  # Повтор того же задания; распознавание ещё не состоялось.
                return _record_result(
                    ctx, _classify_error(exc), requests, started
                )
        _remember_auth(configured, prepared, auth_key)
        result = classify_answer(challenge.code, outcome)
        if result.status is not ProbeStatus.UNCONFIRMED:
            return _record_result(
                ctx, result, requests, started, outcome.finish_reason
            )
        previous = challenge.code
    return _record_result(
        ctx, result, requests, started, outcome.finish_reason
    )
