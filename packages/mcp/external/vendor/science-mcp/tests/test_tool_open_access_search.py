from unittest.mock import AsyncMock, patch

import httpx
import pytest

from science_mcp.cache import DiskCache
from science_mcp.config import load_config
from science_mcp.models import WorkRecord
from science_mcp.throttle import Throttler
from science_mcp.tools import open_access_search


@pytest.mark.asyncio
async def test_invalid_query(tmp_path):
    client = httpx.AsyncClient()
    out = await open_access_search.run(
        client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
        query="", limit=5, offset=0,
    )
    await client.aclose()
    assert out["ok"] is False


@pytest.mark.asyncio
async def test_core_results_returned(tmp_path):
    fake = [WorkRecord(source="core", id="c1", title="A", is_open_access=True)]
    with patch("science_mcp.tools.open_access_search.core_adapter.search",
               AsyncMock(return_value=fake)):
        client = httpx.AsyncClient()
        out = await open_access_search.run(
            client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
            query="x", limit=5, offset=0,
        )
        await client.aclose()
    assert out["ok"] is True
    assert out["sources_queried"] == ["core"]
    assert out["results"][0]["source"] == "core"
