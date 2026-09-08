from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from science_mcp.cache import DiskCache
from science_mcp.config import Config, load_config
from science_mcp.http_client import build_client
from science_mcp.throttle import Throttler
from science_mcp.tools import (
    citations_by_doi,
    explain_sources,
    literature_search,
    ncbi_fetch,
    open_access_search,
    preprint_search,
    work_lookup,
)

log = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    config: Config
    throttler: Throttler
    cache: DiskCache


def build_runtime(config: Optional[Config] = None) -> ServerConfig:
    cfg = config or load_config()
    th = Throttler(queue_timeout_sec=cfg.queue_timeout_sec)
    if cfg.ncbi_api_key:
        th.set_interval("ncbi", 0.1)
    cache = DiskCache(cfg.cache_dir, enabled=cfg.cache_enabled)
    return ServerConfig(config=cfg, throttler=th, cache=cache)


def build_server(srv_cfg: Optional[ServerConfig] = None) -> FastMCP:
    rt = srv_cfg or build_runtime()
    mcp = FastMCP(
        "science-mcp",
        host=rt.config.host,
        port=rt.config.port,
    )

    async def _run_async(coro: Any) -> str:
        result = await coro
        return json.dumps(result, ensure_ascii=False)

    @mcp.tool(name="literature_search")
    async def literature_search_tool(
        query: str,
        sources: Optional[list[str]] = None,
        limit: int = 5,
        offset: int = 0,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        fields: Optional[list[str]] = None,
    ) -> str:
        """Run ONE literature search across journal-article sources. Prefer
        refining `query` over deep pagination. For preprints use preprint_search.
        For a known DOI/PMID use work_lookup.
        Defaults: sources=[openalex,crossref], limit=5, offset=0.
        Constraint: offset+limit<=100.
        `fields` is an optional allowlist from [title, authors, year, abstract,
        doi, url, citation_count]; if set, non-listed fields are dropped from
        results. `source` and `id` are always preserved."""
        async with build_client(rt.config) as client:
            return await _run_async(literature_search.run(
                client, rt.config, rt.throttler, rt.cache,
                query=query, sources=sources, limit=limit, offset=offset,
                year_from=year_from, year_to=year_to, fields=fields,
            ))

    @mcp.tool(name="preprint_search")
    async def preprint_search_tool(
        query: str,
        sources: Optional[list[str]] = None,
        limit: int = 3,
        offset: int = 0,
        arxiv_search_query: Optional[str] = None,
        fetch_biorxiv_details: bool = False,
    ) -> str:
        """Run ONE preprint search (arXiv default; bioRxiv/medRxiv optional).
        arXiv is rate-limited to 1 req / 3s.
        Constraint: offset+limit<=50."""
        async with build_client(rt.config) as client:
            return await _run_async(preprint_search.run(
                client, rt.config, rt.throttler, rt.cache,
                query=query, sources=sources, limit=limit, offset=offset,
                arxiv_search_query=arxiv_search_query,
                fetch_biorxiv_details=fetch_biorxiv_details,
            ))

    @mcp.tool(name="open_access_search")
    async def open_access_search_tool(
        query: str, limit: int = 5, offset: int = 0, include_pmc: bool = False,
    ) -> str:
        """Run ONE open-access-only search via CORE (+ optional PMC)."""
        async with build_client(rt.config) as client:
            return await _run_async(open_access_search.run(
                client, rt.config, rt.throttler, rt.cache,
                query=query, limit=limit, offset=offset, include_pmc=include_pmc,
            ))

    @mcp.tool(name="work_lookup")
    async def work_lookup_tool(
        id: str,
        id_type: str = "auto",
        prefer_sources: Optional[list[str]] = None,
    ) -> str:
        """Resolve metadata by identifier (DOI, PMID, PMCID, arXiv id,
        OpenAlex W..., or s2:... )."""
        async with build_client(rt.config) as client:
            return await _run_async(work_lookup.run(
                client, rt.config, rt.throttler, rt.cache,
                id=id, id_type=id_type, prefer_sources=prefer_sources,
            ))

    @mcp.tool(name="ncbi_fetch")
    async def ncbi_fetch_tool(
        db: str = "pubmed",
        mode: str = "summary",
        term: Optional[str] = None,
        ids: Optional[list[str]] = None,
        retmax: int = 5,
        retstart: int = 0,
        retmode: str = "json",
    ) -> str:
        """Search/fetch PubMed/PMC records.
        mode=summary (default) - lightweight JSON, by ids.
        mode=fetch - full XML records, capped at 50KB total.
        mode=search - esearch returning idlist by term."""
        async with build_client(rt.config) as client:
            return await _run_async(ncbi_fetch.run(
                client, rt.config, rt.throttler, rt.cache,
                db=db, mode=mode, term=term, ids=ids,
                retmax=retmax, retstart=retstart, retmode=retmode,
            ))

    @mcp.tool(name="citations_by_doi")
    async def citations_by_doi_tool(
        doi: str, include: Optional[list[str]] = None, limit: int = 10,
    ) -> str:
        """Citation data for a DOI via OpenCitations.
        include=[count] (default) - fast count only.
        include=[citing] / [cited] - list of edges (cached 24h)."""
        async with build_client(rt.config) as client:
            return await _run_async(citations_by_doi.run(
                client, rt.config, rt.throttler, rt.cache,
                doi=doi, include=include, limit=limit,
            ))

    @mcp.tool(name="explain_sources")
    def explain_sources_tool() -> str:
        """List configured sources, rate limits, key_configured booleans.
        Returns NO secrets (only yes/no flags)."""
        return json.dumps(explain_sources.run(rt.config), ensure_ascii=False)

    return mcp
