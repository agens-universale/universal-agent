from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

import httpx

from science_mcp.adapters import core as core_adapter
from science_mcp.adapters import crossref, dblp, openalex, pubmed, semantic_scholar
from science_mcp.cache import DiskCache
from science_mcp.config import Config
from science_mcp.errors import ScienceMCPError
from science_mcp.models import ToolError, ToolResponse, WorkRecord
from science_mcp.throttle import Throttler

SOURCE_ADAPTERS = {
    "openalex": openalex.search,
    "crossref": crossref.search,
    "pubmed": pubmed.search,
    "semantic_scholar": semantic_scholar.search,
    "dblp": dblp.search,
    "core": core_adapter.search,
}

DEFAULT_SOURCES = ["openalex", "crossref"]

PROJECTABLE_FIELDS = {"title", "authors", "year", "abstract", "doi", "url", "citation_count"}

_TITLE_NORM = re.compile(r"[^a-z0-9]+")


def _dedup_key(rec: WorkRecord) -> str:
    if rec.doi:
        return f"doi:{rec.doi.lower()}"
    title = _TITLE_NORM.sub(" ", (rec.title or "").lower()).strip()
    return f"ty:{title}|{rec.year or ''}"


def _project(rec: WorkRecord, fields: set[str]) -> WorkRecord:
    cleared: dict[str, Any] = {}
    for f in PROJECTABLE_FIELDS - fields:
        cleared[f] = [] if f == "authors" else None
    return rec.model_copy(update=cleared)


async def run(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    query: str,
    sources: Optional[list[str]] = None,
    limit: int = 5,
    offset: int = 0,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    fields: Optional[list[str]] = None,
) -> dict[str, Any]:
    if not query or len(query) > 500:
        return ToolError(error="invalid_argument", message="query length 1..500").model_dump()
    if offset + limit > 100:
        return ToolError(error="invalid_argument", message="offset+limit must be <=100").model_dump()
    use_sources = sources or DEFAULT_SOURCES
    unknown = [s for s in use_sources if s not in SOURCE_ADAPTERS]
    if unknown:
        return ToolError(error="invalid_argument", message=f"unknown sources: {unknown}").model_dump()
    fields_set: Optional[set[str]] = None
    if fields is not None:
        unknown_fields = [f for f in fields if f not in PROJECTABLE_FIELDS]
        if unknown_fields:
            return ToolError(
                error="invalid_argument",
                message=f"unknown fields: {unknown_fields}. Allowed: {sorted(PROJECTABLE_FIELDS)}",
            ).model_dump()
        fields_set = set(fields)

    async def _call(src: str) -> tuple[str, list[WorkRecord], Optional[str]]:
        try:
            fn = SOURCE_ADAPTERS[src]
            kwargs: dict[str, Any] = {"query": query, "limit": limit + offset, "offset": 0}
            if src == "openalex":
                kwargs.update(year_from=year_from, year_to=year_to)
            recs = await fn(client, config, throttler, cache, **kwargs)
            return src, recs, None
        except ScienceMCPError as e:
            return src, [], f"{src}: {e.category.value}: {str(e)}"

    results = await asyncio.gather(*(_call(s) for s in use_sources))
    merged: list[WorkRecord] = []
    seen: set[str] = set()
    warnings: list[str] = []
    for src, recs, warn in results:
        if warn:
            warnings.append(warn)
        for r in recs:
            k = _dedup_key(r)
            if k in seen:
                continue
            seen.add(k)
            merged.append(r)
    merged.sort(key=lambda r: (r.citation_count is None, -(r.citation_count or 0)))
    page = merged[offset:offset + limit]
    if fields_set is not None:
        page = [_project(r, fields_set) for r in page]
    return ToolResponse(
        query=query,
        limit=limit,
        offset=offset,
        has_more=len(merged) > offset + limit,
        sources_queried=use_sources,
        results=page,
        warnings=warnings,
    ).model_dump()
