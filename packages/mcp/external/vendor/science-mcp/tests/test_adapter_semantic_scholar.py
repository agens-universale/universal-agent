import httpx
import pytest

from science_mcp.adapters import semantic_scholar
from science_mcp.cache import DiskCache
from science_mcp.config import load_config
from science_mcp.errors import ScienceMCPError
from science_mcp.throttle import Throttler


@pytest.mark.asyncio
async def test_s2_without_key_raises_unauthorized(monkeypatch, tmp_path):
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(ScienceMCPError) as ei:
        await semantic_scholar.search(
            client, load_config(), Throttler({"semantic_scholar": 0.0}),
            DiskCache(tmp_path, enabled=False),
            query="x", limit=1, offset=0,
        )
    await client.aclose()
    assert ei.value.category.value == "unauthorized"
