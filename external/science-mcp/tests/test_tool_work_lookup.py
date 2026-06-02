from science_mcp.tools.work_lookup import _detect_id_type


def test_detect_doi():
    assert _detect_id_type("10.1038/s41586-020-2649-2") == "doi"


def test_detect_pmid_prefix():
    assert _detect_id_type("PMID:37200000") == "pmid"


def test_detect_pmid_numeric():
    assert _detect_id_type("37200000") == "pmid"


def test_detect_pmcid():
    assert _detect_id_type("PMC1234567") == "pmcid"


def test_detect_arxiv_id():
    assert _detect_id_type("2301.00000") == "arxiv"
    assert _detect_id_type("arxiv:2301.00000v2") == "arxiv"


def test_detect_openalex():
    assert _detect_id_type("W2741809807") == "openalex"


def test_detect_s2():
    assert _detect_id_type("s2:abc123") == "semantic_scholar"


from unittest.mock import AsyncMock, patch

import httpx
import pytest

from science_mcp.cache import DiskCache
from science_mcp.config import load_config
from science_mcp.models import WorkRecord
from science_mcp.throttle import Throttler
from science_mcp.tools import work_lookup


@pytest.mark.asyncio
async def test_doi_falls_back_to_openalex_when_crossref_misses(tmp_path):
    fake_oa = WorkRecord(source="openalex", id="W1", title="From OpenAlex", doi="10.1/x")
    with patch(
        "science_mcp.tools.work_lookup.crossref.lookup_doi",
        AsyncMock(return_value=None),
    ), patch(
        "science_mcp.tools.work_lookup.openalex.lookup_doi",
        AsyncMock(return_value=fake_oa),
    ):
        client = httpx.AsyncClient()
        out = await work_lookup.run(
            client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
            id="10.1/x",
        )
        await client.aclose()
    assert out["source"] == "openalex"
    assert out["doi"] == "10.1/x"


@pytest.mark.asyncio
async def test_doi_uses_first_successful_source(tmp_path):
    fake_cr = WorkRecord(source="crossref", id="10.1/y", title="From CrossRef", doi="10.1/y")
    oa_mock = AsyncMock()
    with patch(
        "science_mcp.tools.work_lookup.crossref.lookup_doi",
        AsyncMock(return_value=fake_cr),
    ), patch(
        "science_mcp.tools.work_lookup.openalex.lookup_doi",
        oa_mock,
    ):
        client = httpx.AsyncClient()
        out = await work_lookup.run(
            client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
            id="10.1/y",
        )
        await client.aclose()
    assert out["source"] == "crossref"
    oa_mock.assert_not_called()


@pytest.mark.asyncio
async def test_doi_all_sources_fail_returns_not_found(tmp_path):
    with patch(
        "science_mcp.tools.work_lookup.crossref.lookup_doi",
        AsyncMock(return_value=None),
    ), patch(
        "science_mcp.tools.work_lookup.openalex.lookup_doi",
        AsyncMock(return_value=None),
    ):
        client = httpx.AsyncClient()
        out = await work_lookup.run(
            client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
            id="10.1/missing",
        )
        await client.aclose()
    assert out["ok"] is False
    assert out["error"] == "not_found"


@pytest.mark.asyncio
async def test_doi_respects_prefer_sources_order(tmp_path):
    fake_oa = WorkRecord(source="openalex", id="W2", title="OA first", doi="10.1/z")
    cr_mock = AsyncMock(return_value=None)
    oa_mock = AsyncMock(return_value=fake_oa)
    with patch(
        "science_mcp.tools.work_lookup.crossref.lookup_doi", cr_mock,
    ), patch(
        "science_mcp.tools.work_lookup.openalex.lookup_doi", oa_mock,
    ):
        client = httpx.AsyncClient()
        out = await work_lookup.run(
            client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
            id="10.1/z",
            prefer_sources=["openalex", "crossref"],
        )
        await client.aclose()
    oa_mock.assert_called_once()
    cr_mock.assert_not_called()
    assert out["source"] == "openalex"


@pytest.mark.asyncio
async def test_pmcid_branch(tmp_path):
    fake_pmc = WorkRecord(source="pubmed", id="123", title="From PMC", pmcid="PMC1234567")
    with patch(
        "science_mcp.tools.work_lookup.pubmed.efetch_works",
        AsyncMock(return_value=[fake_pmc]),
    ):
        client = httpx.AsyncClient()
        out = await work_lookup.run(
            client, load_config(), Throttler({}), DiskCache(tmp_path, enabled=False),
            id="PMC1234567",
        )
        await client.aclose()
    assert out["title"] == "From PMC"
