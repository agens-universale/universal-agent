"""Data-URL картинок на проводе: сборка для запроса и маскировка для логов.

Единственное место, знающее формат ``data:<mime>;base64,<данные>``: проекция
вложений собирает по нему части ``image_url``, логи запросов его маскируют.
Технический слой — ни политики, ни знания о вложениях.
"""

from __future__ import annotations

import base64
import re
from typing import Any

_DATA_URL_RE = re.compile(r"data:([\w.+-]+/[\w.+-]+);base64,([A-Za-z0-9+/=]+)")


def data_url(mime: str, data: bytes) -> str:
    """``data:<mime>;base64,<данные>`` — картинка внутри запроса chat/completions."""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def mask_data_urls(value: Any) -> Any:
    """Копия ``value``, где каждый data-URL стал ``data:<mime>;base64,<N bytes>``
    (N — длина base64-данных). Вход не мутирует; строки без ``base64,`` и
    не-контейнеры возвращаются как есть."""
    if isinstance(value, str):
        if "base64," not in value:
            return value
        return _DATA_URL_RE.sub(
            lambda match: (
                f"data:{match.group(1)};base64,<{len(match.group(2))} bytes>"
            ),
            value,
        )
    if isinstance(value, dict):
        return {key: mask_data_urls(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_data_urls(item) for item in value]
    return value
