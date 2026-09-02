from __future__ import annotations

from typing import Any

import httpx

from science_mcp.adapters import core as core_adapter
from science_mcp.adapters import pmc
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
    limit: int = 5,
    offset: int = 0,
    include_pmc: bool = False,
) -> dict[str, Any]:
    if not query or len(query) > 500:
        return ToolError(error="invalid_argument", message="query length 1..500").model_dump()
    if offset + limit > 100:
        return ToolError(error="invalid_argument", message="offset+limit must be <=100").model_dump()

    sources_used = ["core"]
    results: list[WorkRecord] = []
    warnings: list[str] = []
    try:
        results.extend(await core_adapter.search(
            client, config, throttler, cache,
            query=query, limit=limit + offset, offset=0,
        ))
    except ScienceMCPError as e:
        warnings.append(f"core: {e.category.value}: {str(e)}")
    if include_pmc:
        sources_used.append("pmc")
        try:
            results.extend(await pmc.search(
                client, config, throttler, cache,
                query=query, limit=limit + offset, offset=0,
            ))
        except ScienceMCPError as e:
            warnings.append(f"pmc: {e.category.value}: {str(e)}")
    page = results[offset:offset + limit]
    return ToolResponse(
        query=query, limit=limit, offset=offset,
        has_more=len(results) > offset + limit,
        sources_queried=sources_used, results=page, warnings=warnings,
    ).model_dump()
