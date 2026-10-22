"""Путь эндпоинта в адресе модели.

Ядро шлёт запрос ровно по адресу записи, путь не дописывает: адрес без
`/chat/completions` (или `/responses`) даёт 404, который выглядит как
недоступная модель. Форма такой адрес не запрещает — у прокси бывает
свой путь, — а подсказывает полный."""

from __future__ import annotations

_ENDPOINT_PATHS = {
    "chat_completions": "/chat/completions",
    "responses": "/responses",
}


def endpoint_path_hint(url: str, api_mode: str) -> str | None:
    """Полный адрес, если ``url`` не оканчивается путём эндпоинта режима
    ``api_mode``; иначе (и для ссылки на переменную окружения) — None."""
    suffix = _ENDPOINT_PATHS.get(api_mode)
    url = (url or "").strip()
    if not suffix or not url or "${" in url:
        return None
    base = url.rstrip("/")
    if base.endswith(suffix):
        return None
    return base + suffix
