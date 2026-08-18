from __future__ import annotations

from typing import Any, Literal, Optional

import httpx

from science_mcp.adapters import pubmed
from science_mcp.cache import DiskCache
from science_mcp.config import Config
from science_mcp.errors import ErrorCategory, ScienceMCPError
from science_mcp.models import ToolError, ToolResponse
from science_mcp.throttle import Throttler

MAX_FETCH_BYTES = 50_000


async def run(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    db: Literal["pubmed", "pmc"] = "pubmed",
    mode: Literal["search", "fetch", "summary"] = "summary",
    term: Optional[str] = None,
    ids: Optional[list[str]] = None,
    retmax: int = 5,
    retstart: int = 0,
    retmode: Literal["json", "xml"] = "json",
) -> dict[str, Any]:
    try:
        if mode == "search":
            if not term:
                return ToolError(error="invalid_argument", message="term required when mode=search").model_dump()
            id_list = await pubmed.esearch_ids(
                client, config, throttler, cache,
                db=db, term=term, retmax=retmax, retstart=retstart,
            )
            return {"ok": True, "ids": id_list, "db": db}

        if mode == "summary":
            if not ids:
                return ToolError(error="invalid_argument", message="ids[] required when mode=summary").model_dump()
            summary = await pubmed.esummary(client, config, throttler, cache, db=db, ids=ids)
            return {"ok": True, "summary": summary, "db": db}

        if mode == "fetch":
            if not ids:
                return ToolError(error="invalid_argument", message="ids[] required when mode=fetch").model_dump()
            records = await pubmed.efetch_works(client, config, throttler, cache, db=db, ids=ids)
            payload = ToolResponse(results=records, sources_queried=[db]).model_dump()
            size = len(str(payload).encode("utf-8"))
            if size > MAX_FETCH_BYTES:
                raise ScienceMCPError(
                    ErrorCategory.RESPONSE_TOO_LARGE,
                    source="ncbi", size_bytes=size, max_bytes=MAX_FETCH_BYTES,
                )
            return payload

        return ToolError(error="invalid_argument", message=f"unknown mode: {mode}").model_dump()
    except ScienceMCPError as e:
        return ToolError(
            error=e.category.value, source=e.source or "ncbi", message=str(e)
        ).model_dump()
