"""PMC reuses pubmed.esearch_ids/efetch_works/esummary with db='pmc'.
Kept as a distinct module for adapter-registry consistency. Full-text
efetch is routed via ncbi_fetch tool (which caps the response size)
rather than literature_search."""
from __future__ import annotations

import httpx

from science_mcp.adapters import pubmed
from science_mcp.cache import DiskCache
from science_mcp.config import Config
from science_mcp.models import WorkRecord
from science_mcp.throttle import Throttler


async def search(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    query: str,
    limit: int,
    offset: int,
) -> list[WorkRecord]:
    ids = await pubmed.esearch_ids(
        client, config, throttler, cache,
        db="pmc", term=query, retmax=limit, retstart=offset,
    )
    return [
        WorkRecord(source="pmc", id=pmcid, title="(metadata via ncbi_fetch)", pmcid=pmcid)
        for pmcid in ids
    ]
