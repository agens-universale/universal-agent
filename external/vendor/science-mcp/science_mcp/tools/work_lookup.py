from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, Literal, Optional

import httpx

from science_mcp.adapters import arxiv, crossref, openalex, pubmed, semantic_scholar
from science_mcp.cache import DiskCache
from science_mcp.config import Config
from science_mcp.errors import ScienceMCPError
from science_mcp.models import ToolError, WorkRecord
from science_mcp.throttle import Throttler

IdType = Literal["auto", "doi", "pmid", "pmcid", "arxiv", "openalex", "semantic_scholar"]

DEFAULT_PREFER_SOURCES = ["crossref", "openalex", "semantic_scholar"]


def _detect_id_type(raw: str) -> IdType:
    s = raw.strip()
    if s.startswith("10."):
        return "doi"
    if s.lower().startswith("pmid:") or s.isdigit():
        return "pmid"
    if s.upper().startswith("PMC"):
        return "pmcid"
    if s.lower().startswith("arxiv:") or re.match(r"^\d{4}\.\d{4,5}(v\d+)?$", s):
        return "arxiv"
    if s.upper().startswith("W") and s[1:].isdigit():
        return "openalex"
    if s.lower().startswith("s2:"):
        return "semantic_scholar"
    return "doi"


DoiResolver = Callable[..., Awaitable[Optional[WorkRecord]]]


def _doi_resolvers() -> dict[str, DoiResolver]:
    """Built per-call so monkeypatching adapter attributes is observable."""
    return {
        "crossref": crossref.lookup_doi,
        "openalex": openalex.lookup_doi,
        "semantic_scholar": semantic_scholar.lookup_doi,
    }


async def run(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    id: str,
    id_type: IdType = "auto",
    prefer_sources: Optional[list[str]] = None,
) -> dict[str, Any]:
    if not id:
        return ToolError(error="invalid_argument", message="id is required").model_dump()
    kind: IdType = _detect_id_type(id) if id_type == "auto" else id_type
    sources = prefer_sources or DEFAULT_PREFER_SOURCES
    try:
        if kind == "doi":
            resolvers = _doi_resolvers()
            for src in sources:
                resolver = resolvers.get(src)
                if resolver is None:
                    continue
                rec = await resolver(client, config, throttler, cache, doi=id)
                if rec:
                    return rec.model_dump()
            return ToolError(error="not_found", message=f"id not resolved: {id}").model_dump()
        if kind == "pmid":
            pmid = id.split(":")[-1]
            recs = await pubmed.efetch_works(
                client, config, throttler, cache, db="pubmed", ids=[pmid]
            )
            if recs:
                return recs[0].model_dump()
        if kind == "pmcid":
            pmcid = id.upper().replace("PMC", "")
            recs = await pubmed.efetch_works(
                client, config, throttler, cache, db="pmc", ids=[pmcid],
            )
            if recs:
                return recs[0].model_dump()
        if kind == "arxiv":
            arxiv_id = id.split(":")[-1].split("v")[0]
            recs = await arxiv.search(
                client, config, throttler, cache,
                query="", limit=1, offset=0, raw_search_query=f"id:{arxiv_id}",
            )
            if recs:
                return recs[0].model_dump()
        if kind == "openalex":
            rec = await openalex.lookup_work_id(
                client, config, throttler, cache, work_id=id
            )
            if rec:
                return rec.model_dump()
        return ToolError(error="not_found", message=f"id not resolved: {id}").model_dump()
    except ScienceMCPError as e:
        return ToolError(
            error=e.category.value, source=e.source or kind, message=str(e)
        ).model_dump()
