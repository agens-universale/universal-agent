"""Фейковый транспорт запросов к модели для тестов (async-переезд, Задача 9).

Путь к модели с Задачи 9 один — ``httpx.AsyncClient``; синхронный вход остался
шимом через портал. Тесты, которые раньше патчили приватную
``connect_backend._NO_NETRC_SESSION``, подменяют теперь фабрику клиента.

Сюда же вынесен сбор наблюдаемого: параметры клиента (``verify``/``cert``/
``proxies`` — их httpx принимает при СОЗДАНИИ клиента, а не на запросе) и сами
запросы (URL, тело, заголовки, таймаут). Без общего места каждый тест
подсматривал бы за транспортом по-своему, и следующий переезд пришлось бы
чинить в восьми файлах вместо одного.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx


@dataclass
class TransportSpy:
    """Что транспорт увидел: с чем создан клиент и какие ушли запросы."""

    client_kwargs: list[dict] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    timeouts: list[Any] = field(default_factory=list)

    @property
    def last_client_kwargs(self) -> dict:
        assert self.client_kwargs, "клиент так и не был создан"
        return self.client_kwargs[-1]

    @property
    def last_request(self) -> httpx.Request:
        assert self.requests, "запрос так и не ушёл"
        return self.requests[-1]

    @property
    def last_payload(self) -> dict:
        return json.loads(self.last_request.content or b"{}")


def sse_body(*payloads: dict, done: bool = True) -> str:
    """Тело SSE-стрима из готовых чанков."""
    body = "".join(
        f"data: {json.dumps(p, ensure_ascii=False)}\n\n" for p in payloads
    )
    return body + ("data: [DONE]\n\n" if done else "")


def json_response(
    payload: dict, status: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json=payload)


def stream_response(
    body: str, status: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(
        status, text=body, headers={"content-type": "text/event-stream"}
    )


def install_transport(
    monkeypatch,
    handler: Optional[Callable[[httpx.Request], httpx.Response]] = None,
) -> TransportSpy:
    """Подменить ``connect_backend.get_async_client`` фейком.

    ``handler`` — что отвечать; по умолчанию минимальный валидный ответ модели.
    Клиент создаётся на каждый вызов: MockTransport состояния не держит, а
    общий кэш пер-loop клиентов трогать из теста не следует.
    """
    from gc_core.utils import connect_backend

    spy = TransportSpy()
    if handler is None:
        handler = json_response(
            {"id": "x", "choices": [{"message": {"content": "ok"}}]}
        )

    def _recording_handler(request: httpx.Request) -> httpx.Response:
        spy.requests.append(request)
        return handler(request)

    class _SpyClient(httpx.AsyncClient):
        async def post(self, *args: Any, **kwargs: Any):
            spy.timeouts.append(kwargs.get("timeout"))
            return await super().post(*args, **kwargs)

        def stream(self, *args: Any, **kwargs: Any):
            spy.timeouts.append(kwargs.get("timeout"))
            return super().stream(*args, **kwargs)

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        spy.client_kwargs.append(kwargs)
        return _SpyClient(transport=httpx.MockTransport(_recording_handler))

    monkeypatch.setattr(connect_backend, "get_async_client", _factory)
    return spy
