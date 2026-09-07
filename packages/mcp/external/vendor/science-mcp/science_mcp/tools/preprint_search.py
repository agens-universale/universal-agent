from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from science_mcp.adapters import arxiv, cshl_preprints
from science_mcp.cache import DiskCache
from science_mcp.config import Config
from science_mcp.errors import ScienceMCPError
from science_mcp.models import ToolError, ToolResponse, WorkRecord
from science_mcp.throttle import Throttler


async def run(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    query: str,
    sources: Optional[list[str]] = None,
    limit: int = 3,
    offset: int = 0,
    arxiv_search_query: Optional[str] = None,
    fetch_biorxiv_details: bool = False,
) -> dict[str, Any]:
    if not query or len(query) > 500:
        return ToolError(error="invalid_argument", message="query length 1..500").model_dump()
    if offset + limit > 50:
        return ToolError(error="invalid_argument", message="offset+limit must be <=50").model_dump()
    use = sources or ["arxiv"]

    async def _call(src: str) -> tuple[str, list[WorkRecord], Optional[str]]:
        try:
            if src == "arxiv":
                recs = await arxiv.search(
                    client, config, throttler, cache,
                    query=query, limit=limit + offset, offset=0,
                    raw_search_query=arxiv_search_query,
                )
            elif src in ("biorxiv", "medrxiv"):
                recs = await cshl_preprints.search(
                    client, config, throttler, cache,
                    query=query, limit=limit + offset, offset=0,
                    server=src, fetch_details=fetch_biorxiv_details,
                )
            else:
                return src, [], f"unknown source: {src}"
            return src, recs, None
        except ScienceMCPError as e:
            return src, [], f"{src}: {e.category.value}: {str(e)}"

    out = await asyncio.gather(*(_call(s) for s in use))
    merged: list[WorkRecord] = []
    warnings: list[str] = []
    for src, recs, warn in out:
        if warn:
            warnings.append(warn)
        merged.extend(recs)
    page = merged[offset:offset + limit]
    return ToolResponse(
        query=query, limit=limit, offset=offset,
        has_more=len(merged) > offset + limit,
        sources_queried=use, results=page, warnings=warnings,
    ).model_dump()
