from unittest.mock import AsyncMock, patch

import httpx
import pytest

from science_mcp.cache import DiskCache
from science_mcp.config import load_config
from science_mcp.throttle import Throttler
from science_mcp.tools import citations_by_doi


@pytest.mark.asyncio
async def test_invalid_doi(tmp_path):
    client = httpx.AsyncClient()
    out = await citations_by_doi.run(
        client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
        doi="not-a-doi",
    )
    await client.aclose()
    assert out["ok"] is False


@pytest.mark.asyncio
async def test_default_include_count_only(tmp_path):
    with patch("science_mcp.tools.citations_by_doi.opencitations.citation_count",
               AsyncMock(return_value=42)), \
         patch("science_mcp.tools.citations_by_doi.opencitations.citations_list",
               AsyncMock()) as edges:
        client = httpx.AsyncClient()
        out = await citations_by_doi.run(
            client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
            doi="10.1/x",
        )
        await client.aclose()
    assert out["ok"] is True
    assert out["count"] == 42
    edges.assert_not_called()
