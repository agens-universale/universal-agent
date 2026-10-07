from collections import deque

import httpx
import pytest

from gc_core.utils import connect_backend


@pytest.fixture
async def probe_http(monkeypatch):
    requests, responses = [], deque()

    def handler(request):
        requests.append(request)
        response = responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response(request) if callable(response) else response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        monkeypatch.setattr(
            connect_backend, "get_async_client", lambda **kwargs: client
        )
        yield requests, responses
