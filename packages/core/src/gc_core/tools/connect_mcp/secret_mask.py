"""Маскировка значений секретов определения MCP-сервера в тексте.

Чистые функции: текст причины отказа и строки лога не должны нести
значения ``token``, ``headers`` и ``env`` определения, а также значения
параметров строки запроса и пароль адреса ``url`` (``?apikey=…``).
"""

import re
from urllib.parse import urlsplit

# Пороги маскировки секретов — см. ``without_secrets``.
_MIN_SECRET_LEN = 8
_MIN_WORD_SECRET_LEN = 4
_SECRET_MASK = "***"


def _masked_param(pair: str) -> str:
    """``ключ=значение`` → ``ключ=***``; параметр без ``=`` — весь."""
    name, sep, value = pair.partition("=")
    if not sep:
        return _SECRET_MASK if pair else pair
    return f"{name}={_SECRET_MASK}" if value else pair


def _without_url_secrets(text: str, url: object) -> str:
    """Секреты адреса — по месту: сырая строка запроса (как записана в
    определении) заменяется той же со ``***`` вместо значений, пароль
    ``user:pass@`` — на ``user:***@``. Хост, порт и путь не трогаются."""
    if not isinstance(url, str):
        return text
    parts = urlsplit(url)
    if parts.query:
        masked = "&".join(_masked_param(p) for p in parts.query.split("&"))
        text = text.replace(f"?{parts.query}", f"?{masked}")
    userinfo, at, _host = parts.netloc.rpartition("@")
    user, colon, _password = userinfo.partition(":")
    if at and colon:
        text = text.replace(f"{userinfo}@", f"{user}:{_SECRET_MASK}@")
    return text


def _secret_fragments(server_cfg: dict) -> set[str]:
    """Значения ``token``, ``headers`` и ``env`` целиком и их части между
    пробелами (``Bearer <ключ>`` — ещё и ``<ключ>``)."""
    values = [server_cfg.get("token")]
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

    Сначала адрес — по месту (``_without_url_secrets``): строка запроса и
    пароль в том виде, в каком записаны, без раскодирования. Затем
    ``token``, ``headers`` и ``env``: фрагмент от 8 символов заменяется
    везде, где встретился; от 4 до 7 — только отдельным словом (слева и
    справа не буква, не цифра и не подчёркивание), чтобы не портить числа и
    адреса. Длинные фрагменты идут первыми.

    Не покрыто: фрагменты короче 4 символов и значение из 4–7 символов
    внутри слова; секрет ``token``/``headers``/``env`` в экранированной
    форме (``repr`` с ``\\`` или ``'``, JSON- и URL-кодирование) и в другом
    регистре; адрес, записанный в тексте иначе, чем в определении
    (перекодированный, с другим порядком параметров). Побочный эффект
    порогов: часть значения из 4–7 символов, стоящая отдельным словом,
    маскируется, даже если она не секрет (порт ``8001``, ``Bearer``).
    """
    text = _without_url_secrets(text, server_cfg.get("url"))
    for fragment in sorted(
        _secret_fragments(server_cfg), key=len, reverse=True
    ):
        text = _mask_fragment(text, fragment)
    return text


__all__ = ["without_secrets"]
