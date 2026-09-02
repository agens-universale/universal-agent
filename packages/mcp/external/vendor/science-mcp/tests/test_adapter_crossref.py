import json

import httpx
import pytest

from science_mcp.adapters import crossref
from science_mcp.cache import DiskCache
from science_mcp.config import load_config
from science_mcp.throttle import Throttler


@pytest.mark.asyncio
async def test_crossref_search_normalizes(crossref_sample, tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        assert "query=CRISPR" in str(req.url)
        return httpx.Response(200, json=json.loads(crossref_sample))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await crossref.search(
        client, load_config(), Throttler({"crossref": 0.0}),
        DiskCache(tmp_path, enabled=False),
        query="CRISPR cancer therapy", limit=1, offset=0,
    )
    await client.aclose()
    assert out and out[0].source == "crossref"
    assert out[0].doi
