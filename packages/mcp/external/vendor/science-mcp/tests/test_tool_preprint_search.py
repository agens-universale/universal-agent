import httpx
import pytest

from science_mcp.cache import DiskCache
from science_mcp.config import load_config
from science_mcp.throttle import Throttler
from science_mcp.tools import preprint_search


@pytest.mark.asyncio
async def test_invalid_query(tmp_path):
    client = httpx.AsyncClient()
    out = await preprint_search.run(
        client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
        query="", limit=3, offset=0,
    )
    await client.aclose()
    assert out["ok"] is False
    assert out["error"] == "invalid_argument"


@pytest.mark.asyncio
async def test_offset_plus_limit_over_50(tmp_path):
    client = httpx.AsyncClient()
    out = await preprint_search.run(
        client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
        query="x", limit=10, offset=45,
    )
    await client.aclose()
    assert out["ok"] is False
