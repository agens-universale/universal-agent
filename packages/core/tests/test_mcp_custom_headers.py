import pytest


def test_mcp_server_config_accepts_headers():
    from gc_core.config_modules.mcp import McpServerConfig

    cfg = McpServerConfig.model_validate(
        {
            "transport": "http",
            "url": "http://x",
            "headers": {"Authorization": "Bearer a", "X-API-Key": "k"},
        }
    )
    assert cfg.headers["Authorization"] == "Bearer a"
    assert cfg.headers["X-API-Key"] == "k"


async def test_discovery_http_transport_passes_custom_headers(monkeypatch):
    """
    Проверяем прокидывание headers для transport=http:
    - Authorization из headers переопределяет token
    - кастомные X-* header'ы доходят до transport слоя
    """

    from gc_core.tools.connect_mcp import registry as reg
    from gc_core.tools.connect_mcp import transport as transport_mod

    captured: list[tuple[str, dict[str, str]]] = []

    def fake_request_http(
        url: str,
        headers: dict[str, str],
        json_payload: dict,
        timeout: float,
        verify=True,
        cert=None,
    ):
        method = (json_payload or {}).get("method") or ""
        captured.append((method, dict(headers)))

        if method == "initialize":
            return {"mcp-session-id": "sid"}, {}
        if method == "notifications/initialized":
            return {}, {}
        if method == "tools/list":
            return {}, {
                "result": {
                    "tools": [
                        {
                            "name": "hello",
                            "description": "desc",
                            "inputSchema": {
                                "type": "object",
                                "properties": {},
                                "additionalProperties": False,
                            },
                        }
                    ]
                }
            }
        return {}, {}

    # Дискавери идёт КОРУТИННЫМ транспортом: синхронный путь остался только
    # для долгоживущего stdio. Заголовки собирает тот же `_mcp_headers`,
    # поэтому предмет теста не изменился.
    async def fake_arequest_http(url, headers, json_payload, timeout, **_kw):
        return fake_request_http(url, headers, json_payload, timeout)

    monkeypatch.setattr(transport_mod, "_arequest_http", fake_arequest_http)

    server_cfg = {
        "transport": "http",
        "url": "http://x/mcp",
        "token": "T",
        "headers": {"Authorization": "Bearer CUSTOM", "X-Test": "1"},
    }

    tools, status = await reg._adiscover_one("s", server_cfg, timeout=1.0)
    assert status["status"] == "connected"
    assert any(t.get("name") == "hello" for t in tools)

    assert captured, "expected at least one HTTP request"
    assert all(h.get("Authorization") == "Bearer CUSTOM" for _, h in captured)
    assert all(h.get("X-Test") == "1" for _, h in captured)


def test_stream_http_client_merges_custom_headers(monkeypatch):
    from gc_core.tools.connect_mcp import stream_client as stream_client

    captured: dict[str, dict | None] = {"headers": None}

    class DummyAsyncClient:
        def __init__(self, **kwargs):
            captured["headers"] = kwargs.get("headers")

    monkeypatch.setattr(stream_client.httpx, "AsyncClient", DummyAsyncClient)

    stream_client._make_stream_http_client(
        token="T",
        extra_headers={"Authorization": "Bearer CUSTOM", "X-Test": "1"},
        timeout=1.0,
        verify=True,
        cert=None,
        base_url="http://x",
    )

    assert captured["headers"] is not None
    assert captured["headers"]["Authorization"] == "Bearer CUSTOM"
    assert captured["headers"]["X-Test"] == "1"

