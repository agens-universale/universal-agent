from unittest.mock import AsyncMock, patch

import httpx
import pytest

from science_mcp.cache import DiskCache
from science_mcp.config import load_config
from science_mcp.models import WorkRecord
from science_mcp.throttle import Throttler
from science_mcp.tools import ncbi_fetch


@pytest.mark.asyncio
async def test_search_mode_requires_term(tmp_path):
    client = httpx.AsyncClient()
    out = await ncbi_fetch.run(
        client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
        db="pubmed", mode="search", term=None,
    )
    await client.aclose()
    assert out["ok"] is False
    assert out["error"] == "invalid_argument"


@pytest.mark.asyncio
async def test_fetch_mode_requires_ids(tmp_path):
    client = httpx.AsyncClient()
    out = await ncbi_fetch.run(
        client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
        db="pubmed", mode="fetch", ids=None,
    )
    await client.aclose()
    assert out["ok"] is False


@pytest.mark.asyncio
async def test_fetch_caps_oversized_response(tmp_path):
    big_record = WorkRecord(source="pubmed", id="1", title="x" * 60_000)
    with patch("science_mcp.tools.ncbi_fetch.pubmed.efetch_works",
               AsyncMock(return_value=[big_record])):
        client = httpx.AsyncClient()
        out = await ncbi_fetch.run(
            client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
            db="pubmed", mode="fetch", ids=["123"],
        )
        await client.aclose()
    assert out["ok"] is False
    assert out["error"] == "response_too_large"
