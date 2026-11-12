"""Подпись тела по Standard Webhooks (https://www.standardwebhooks.com).

Подписанный запрос несёт ``webhook-id``, ``webhook-timestamp`` (секунды
эпохи) и ``webhook-signature`` — одну или несколько через пробел подписей
``v1,<base64 HMAC-SHA256("{id}.{timestamp}.{тело}")>``. Секрет —
``whsec_<base64 ключа>``; список секретов — смена секрета без простоя.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Iterable
from typing import Any

PREFIX = "whsec_"


class SignatureError(ValueError):
    """Подписи нет, она чужая или запрос вне окна времени."""


def secret_key(secret: str) -> bytes:
    """Ключ HMAC из секрета ``whsec_…``; негодный — ``ValueError``."""
    raw = secret[len(PREFIX) :] if secret.startswith(PREFIX) else secret
    try:
        key = base64.b64decode(raw, validate=True)
    except ValueError:  # binascii.Error — наследник ValueError
        raise ValueError("секрет подписи — whsec_<base64>") from None
    if not key:
        raise ValueError("секрет подписи пуст")
    return key


def sign(secret: str, msg_id: str, timestamp: int, body: bytes) -> str:
    signed = f"{msg_id}.{timestamp}.".encode() + body
    mac = hmac.new(secret_key(secret), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def verify(
    secrets: Iterable[str],
    headers: Any,
    body: bytes,
    *,
    now: int,
    tolerance: int,
) -> None:
    """Подпись тела верна одним из секретов и запрос в окне ``tolerance``
    секунд от ``now`` — иначе ``SignatureError``."""
    msg_id = headers.get("webhook-id")
    stamp = headers.get("webhook-timestamp")
    signature = headers.get("webhook-signature")
    if not (msg_id and stamp and signature):
        raise SignatureError("missing webhook signature headers")
    try:
        timestamp = int(stamp)
    except ValueError:
        raise SignatureError("invalid webhook-timestamp") from None
    if abs(now - timestamp) > tolerance:
        raise SignatureError("webhook-timestamp outside the tolerance")
    expected = [sign(s, msg_id, timestamp, body) for s in secrets]
    for candidate in signature.split():
        if any(hmac.compare_digest(candidate, e) for e in expected):
            return
    raise SignatureError("no matching webhook signature")
