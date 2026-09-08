import json

import httpx
import pytest

from science_mcp.adapters import openalex
from science_mcp.cache import DiskCache
from science_mcp.config import load_config
from science_mcp.throttle import Throttler


@pytest.mark.asyncio
async def test_openalex_search_normalizes_with_mock(openalex_sample, tmp_path):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=json.loads(openalex_sample))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await openalex.search(
        client, load_config(), Throttler({"openalex": 0.0}),
        DiskCache(tmp_path, enabled=False),
        query="CRISPR cancer therapy", limit=1, offset=0,
    )
    await client.aclose()
    assert "search=CRISPR" in captured["url"]
    assert "mailto=team%40company.example" in captured["url"]
    assert len(out) >= 1
    assert out[0].source == "openalex"
