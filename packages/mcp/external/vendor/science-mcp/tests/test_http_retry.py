
import httpx
import pytest

from science_mcp.errors import ScienceMCPError
from science_mcp.http_client import request_with_retry
from science_mcp.throttle import Throttler


@pytest.mark.asyncio
async def test_retry_succeeds_after_429(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="rate limited")
        return httpx.Response(200, json={"ok": True})

    sleeps: list[float] = []

    async def fast_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("science_mcp.http_client.asyncio.sleep", fast_sleep)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    th = Throttler({"openalex": 0.0})

    resp = await request_with_retry(
        client, "GET", "https://api.openalex.org/works",
        source="openalex", throttler=th, params={"q": "x"},
    )
    await client.aclose()

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert calls["n"] == 2
    assert sleeps == [0]


@pytest.mark.asyncio
async def test_no_retry_on_4xx_other_than_429(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="not found")

    async def fast_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("science_mcp.http_client.asyncio.sleep", fast_sleep)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    th = Throttler({"openalex": 0.0})

    with pytest.raises(ScienceMCPError):
        await request_with_retry(
            client, "GET", "https://api.openalex.org/works",
            source="openalex", throttler=th,
        )
    await client.aclose()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_retries_on_500_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="err")
        return httpx.Response(200, json={"ok": True})

    sleeps: list[float] = []

    async def fast_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("science_mcp.http_client.asyncio.sleep", fast_sleep)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    th = Throttler({"crossref": 0.0})

    resp = await request_with_retry(
        client, "GET", "https://api.crossref.org/works",
        source="crossref", throttler=th,
    )
    await client.aclose()
    assert resp.status_code == 200
    assert calls["n"] == 2
    assert sleeps == [30.0]


@pytest.mark.asyncio
async def test_arxiv_pause_90s(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, text="<feed></feed>")

    sleeps: list[float] = []

    async def fast_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("science_mcp.http_client.asyncio.sleep", fast_sleep)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    th = Throttler({"arxiv": 0.0})

    resp = await request_with_retry(
        client, "GET", "https://export.arxiv.org/api/query",
        source="arxiv", throttler=th,
    )
    await client.aclose()
    assert resp.status_code == 200
    assert sleeps == [90.0]


@pytest.mark.asyncio
async def test_max_retries_exceeded_raises(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="rate limited")

    async def fast_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("science_mcp.http_client.asyncio.sleep", fast_sleep)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    th = Throttler({"openalex": 0.0})

    with pytest.raises(ScienceMCPError) as ei:
        await request_with_retry(
            client, "GET", "https://api.openalex.org/works",
            source="openalex", throttler=th,
        )
    await client.aclose()
    assert ei.value.category.value == "rate_limited"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_semantic_scholar_exp_backoff(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json={"data": []})

    sleeps: list[float] = []

    async def fast_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("science_mcp.http_client.asyncio.sleep", fast_sleep)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    th = Throttler({"semantic_scholar": 0.0})

    resp = await request_with_retry(
        client, "GET", "https://api.semanticscholar.org/graph/v1/paper/search",
        source="semantic_scholar", throttler=th,
    )
    await client.aclose()
    assert resp.status_code == 200
    assert sleeps == [2.0, 4.0]


@pytest.mark.asyncio
async def test_connect_timeout_wrapped_to_timeout_category(monkeypatch):
    """httpx.ConnectTimeout must surface as ScienceMCPError(TIMEOUT).

    Regression: previously leaked as raw exception through FastMCP, the agent
    saw an opaque `isError=true` with no actionable category.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("cold tls handshake")

    async def fast_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("science_mcp.http_client.asyncio.sleep", fast_sleep)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    th = Throttler({"opencitations": 0.0})

    with pytest.raises(ScienceMCPError) as ei:
        await request_with_retry(
            client, "GET", "https://api.opencitations.net/index/v1/citations/10.x",
            source="opencitations", throttler=th,
        )
    await client.aclose()
    assert ei.value.category.value == "timeout"
    assert ei.value.source == "opencitations"


@pytest.mark.asyncio
async def test_read_timeout_wrapped_to_timeout_category(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream slow")

    async def fast_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("science_mcp.http_client.asyncio.sleep", fast_sleep)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    th = Throttler({"crossref": 0.0})

    with pytest.raises(ScienceMCPError) as ei:
        await request_with_retry(
            client, "GET", "https://api.crossref.org/works",
            source="crossref", throttler=th,
        )
    await client.aclose()
    assert ei.value.category.value == "timeout"
    assert ei.value.source == "crossref"


@pytest.mark.asyncio
async def test_network_error_wrapped_to_upstream_error(monkeypatch):
    """Non-timeout network failures must surface as UPSTREAM_ERROR, not leak."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS resolution failed")

    async def fast_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("science_mcp.http_client.asyncio.sleep", fast_sleep)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    th = Throttler({"openalex": 0.0})

    with pytest.raises(ScienceMCPError) as ei:
        await request_with_retry(
            client, "GET", "https://api.openalex.org/works",
            source="openalex", throttler=th,
        )
    await client.aclose()
    assert ei.value.category.value == "upstream_error"
    assert ei.value.source == "openalex"


@pytest.mark.asyncio
async def test_retry_after_header_overrides_default_pause(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "5"}, text="rate limited")
        return httpx.Response(200, json={"ok": True})

    sleeps: list[float] = []

    async def fast_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("science_mcp.http_client.asyncio.sleep", fast_sleep)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    th = Throttler({"dblp": 0.0})

    await request_with_retry(
        client, "GET", "https://dblp.org/search/publ/api",
        source="dblp", throttler=th,
    )
    await client.aclose()
    assert sleeps == [5.0]
