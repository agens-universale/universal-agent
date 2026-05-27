from __future__ import annotations

from typing import Any, Optional

import httpx

from science_mcp.adapters import opencitations
from science_mcp.cache import DiskCache
from science_mcp.config import Config
from science_mcp.errors import ScienceMCPError
from science_mcp.models import ToolError
from science_mcp.throttle import Throttler


async def run(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    doi: str,
    include: Optional[list[str]] = None,
    limit: int = 10,
) -> dict[str, Any]:
    if not doi or not doi.startswith("10."):
        return ToolError(error="invalid_argument", message="doi must start with 10.").model_dump()
    inc = include or ["count"]
    out: dict[str, Any] = {"ok": True, "doi": doi}
    try:
        if "count" in inc:
            out["count"] = await opencitations.citation_count(
                client, config, throttler, cache, doi=doi
            )
        if "citing" in inc or "cited" in inc:
            edges = await opencitations.citations_list(
                client, config, throttler, cache, doi=doi, limit=limit
            )
            if "citing" in inc:
                out["citing"] = [e.get("citing") for e in edges if e.get("citing")]
            if "cited" in inc:
                out["cited"] = [e.get("cited") for e in edges if e.get("cited")]
        return out
    except ScienceMCPError as e:
        return ToolError(
            error=e.category.value, source="opencitations", message=str(e)
        ).model_dump()
