from unittest.mock import AsyncMock, patch

import httpx
import pytest

from science_mcp.cache import DiskCache
from science_mcp.config import load_config
from science_mcp.models import WorkRecord
from science_mcp.throttle import Throttler
from science_mcp.tools import literature_search


def _rec(source, doi, title, citations):
    return WorkRecord(source=source, id=doi, title=title, doi=doi, citation_count=citations)


@pytest.mark.asyncio
async def test_invalid_query_returns_invalid_argument(tmp_path):
    client = httpx.AsyncClient()
    out = await literature_search.run(
        client, load_config(), Throttler({}, queue_timeout_sec=1),
        DiskCache(tmp_path, enabled=False),
        query="", limit=5, offset=0,
    )
    await client.aclose()
    assert out["ok"] is False
    assert out["error"] == "invalid_argument"


@pytest.mark.asyncio
async def test_offset_plus_limit_over_100(tmp_path):
    client = httpx.AsyncClient()
    out = await literature_search.run(
        client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
        query="x", limit=20, offset=90,
    )
    await client.aclose()
    assert out["ok"] is False
    assert out["error"] == "invalid_argument"


@pytest.mark.asyncio
async def test_dedup_by_doi_and_sort_by_citations(tmp_path):
    fake_openalex = [
        _rec("openalex", "10.1/a", "Paper A", 100),
        _rec("openalex", "10.1/b", "Paper B", 5),
    ]
    fake_crossref = [
        _rec("crossref", "10.1/a", "Paper A (dupe)", 100),
        _rec("crossref", "10.1/c", "Paper C", 50),
    ]
    with patch(
        "science_mcp.tools.literature_search.SOURCE_ADAPTERS",
        {
            "openalex": AsyncMock(return_value=fake_openalex),
            "crossref": AsyncMock(return_value=fake_crossref),
        },
    ):
        client = httpx.AsyncClient()
        out = await literature_search.run(
            client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
            query="x", limit=5, offset=0,
        )
        await client.aclose()
    assert out["ok"] is True
    dois = [r["doi"] for r in out["results"]]
    assert dois.count("10.1/a") == 1
    assert dois[0] == "10.1/a"
    assert dois[-1] == "10.1/b"


@pytest.mark.asyncio
async def test_adapter_failure_becomes_warning(tmp_path):
    from science_mcp.errors import ErrorCategory, ScienceMCPError

    failing = AsyncMock(side_effect=ScienceMCPError(ErrorCategory.RATE_LIMITED, source="openalex"))
    ok = AsyncMock(return_value=[_rec("crossref", "10.1/x", "X", 1)])
    with patch(
        "science_mcp.tools.literature_search.SOURCE_ADAPTERS",
        {"openalex": failing, "crossref": ok},
    ):
        client = httpx.AsyncClient()
        out = await literature_search.run(
            client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
            query="x", limit=5, offset=0,
            sources=["openalex", "crossref"],
        )
        await client.aclose()
    assert out["ok"] is True
    assert len(out["results"]) == 1
    assert any("openalex" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_fields_param_projects_results(tmp_path):
    fake = [_rec("openalex", "10.1/a", "Title A", 5)]
    fake[0].year = 2024
    fake[0].abstract = "abstract text"
    fake[0].url = "http://example/a"

    with patch(
        "science_mcp.tools.literature_search.SOURCE_ADAPTERS",
        {"openalex": AsyncMock(return_value=fake)},
    ):
        client = httpx.AsyncClient()
        out = await literature_search.run(
            client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
            query="x", limit=5, offset=0,
            sources=["openalex"],
            fields=["title", "doi"],
        )
        await client.aclose()
    assert out["ok"] is True
    r = out["results"][0]
    assert r["source"] == "openalex"
    assert r["id"] == "10.1/a"
    assert r["title"] == "Title A"
    assert r["doi"] == "10.1/a"
    assert not r.get("year")
    assert not r.get("abstract")
    assert not r.get("url")


@pytest.mark.asyncio
async def test_fields_unknown_returns_invalid_argument(tmp_path):
    client = httpx.AsyncClient()
    out = await literature_search.run(
        client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
        query="x", limit=5, offset=0,
        fields=["title", "no_such_field"],
    )
    await client.aclose()
    assert out["ok"] is False
    assert out["error"] == "invalid_argument"


@pytest.mark.asyncio
async def test_fields_default_returns_all(tmp_path):
    fake = [_rec("openalex", "10.1/a", "Title A", 5)]
    fake[0].abstract = "abstract"
    with patch(
        "science_mcp.tools.literature_search.SOURCE_ADAPTERS",
        {"openalex": AsyncMock(return_value=fake)},
    ):
        client = httpx.AsyncClient()
        out = await literature_search.run(
            client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
            query="x", limit=5, offset=0, sources=["openalex"],
        )
        await client.aclose()
    r = out["results"][0]
    assert r["abstract"] == "abstract"
    assert r["citation_count"] == 5
