"""Маскировка значений секретов определения MCP-сервера в тексте.

Чистые функции: текст причины отказа и строки лога не должны нести
значения ``token``, ``headers`` и ``env`` определения, а также значения
параметров строки запроса и пароль адреса ``url`` (``?apikey=…``).
"""

import re
from urllib.parse import parse_qsl, urlsplit

# Пороги маскировки секретов — см. ``without_secrets``.
_MIN_SECRET_LEN = 8
_MIN_WORD_SECRET_LEN = 4
_SECRET_MASK = "***"


def _url_secrets(url: object) -> list[str]:
    """Значения параметров строки запроса и пароль адреса сервера."""
    if not isinstance(url, str):
        return []
    parts = urlsplit(url)
    values = [value for _, value in parse_qsl(parts.query)]
    return [*values, parts.password or ""]


def _secret_fragments(server_cfg: dict) -> set[str]:
    """Значения ``token``, ``headers``, ``env`` и секреты адреса целиком и
    их части между пробелами (``Bearer <ключ>`` — ещё и ``<ключ>``)."""
    values = [server_cfg.get("token"), *_url_secrets(server_cfg.get("url"))]
    for field in ("headers", "env"):
        values.extend((server_cfg.get(field) or {}).values())
    fragments: set[str] = set()
    for value in values:
        if isinstance(value, str):
            fragments.add(value)
            fragments.update(value.split())
    return {f for f in fragments if len(f) >= _MIN_WORD_SECRET_LEN}


def _mask_fragment(text: str, fragment: str) -> str:
    if len(fragment) >= _MIN_SECRET_LEN:
        return text.replace(fragment, _SECRET_MASK)
    word = rf"(?<!\w){re.escape(fragment)}(?!\w)"
    return re.sub(word, _SECRET_MASK, text)


def without_secrets(text: str, server_cfg: dict) -> str:
    """Текст без значений секретов определения.

    Фрагмент от 8 символов заменяется везде, где встретился; от 4 до 7 —
    только отдельным словом (слева и справа не буква, не цифра и не
    подчёркивание), чтобы не портить числа и адреса. Длинные фрагменты
    идут первыми.

    Не покрыто: фрагменты короче 4 символов и значение из 4–7 символов
    внутри слова; секрет в экранированной форме (``repr`` с ``\\`` или
    ``'``, JSON- и URL-кодирование) и в другом регистре. Побочный эффект
    порогов: часть значения из 4–7 символов, стоящая отдельным словом,
    маскируется, даже если она не секрет (порт ``8001``, ``Bearer``).
    """
    for fragment in sorted(
        _secret_fragments(server_cfg), key=len, reverse=True
    ):
        text = _mask_fragment(text, fragment)
    return text


__all__ = ["without_secrets"]
