"""Ошибка бэкенда в режиме responses должна нести ТЕЛО, а не голый код.

У потокового ответа тело не прочитано до ``aread()``, и ``raise_for_status()``
поднимал ``HTTPStatusError`` с пустым ``response.text``. Для 4xx это ровно та
строка, в которой бэкенд объясняет причину — неизвестная модель, превышен
лимит: в лог уходил один статус, и разбираться было не с чем.

Путь ``chat/completions`` тело читал. Асимметрия и была дефектом.
"""

import logging

import httpx
import pytest
from gc_core.utils import connect_backend


class _Logger(logging.Logger):
    def __init__(self):
        super().__init__("test")
        self.errors: list = []

    def error(self, msg, *args, **kw):
        self.errors.append(msg % args if args else msg)


async def test_responses_error_carries_the_body(monkeypatch):
    body = '{"error": {"message": "модель gpt-невидимка не найдена"}}'

    def _dispatch(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_dispatch))
    monkeypatch.setattr(
        connect_backend, "get_async_client", lambda **kw: client
    )
    logger = _Logger()

    with pytest.raises(httpx.HTTPStatusError):
        await connect_backend.arequest_backend_responses(
            data={"model": "gpt-невидимка"},
            headers={},
            url="https://backend.test/v1/responses",
            use_retry=False,
            timeout=5,
            logger=logger,
        )

    assert any("невидимка" in line for line in logger.errors), (
        f"тело ошибки потеряно, в логе только: {logger.errors}"
    )
