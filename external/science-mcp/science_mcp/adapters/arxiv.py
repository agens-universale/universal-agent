from __future__ import annotations

from typing import Any, Optional

import httpx

from science_mcp.cache import DiskCache, hash_key
from science_mcp.config import Config
from science_mcp.http_client import request_with_retry
from science_mcp.models import WorkRecord
from science_mcp.normalize import arxiv_atom_to_works
from science_mcp.throttle import Throttler

BASE_URL = "https://export.arxiv.org/api/query"


async def search(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    query: str,
    limit: int,
    offset: int,
    raw_search_query: Optional[str] = None,
) -> list[WorkRecord]:
    sq = raw_search_query or f"all:{query}"
    params: dict[str, Any] = {
        "search_query": sq, "start": offset, "max_results": limit,
    }
    key = hash_key("arxiv", BASE_URL, params)
    cached = cache.get(key)
    if cached is not None:
        xml_text = cached
    else:
        resp = await request_with_retry(
            client, "GET", BASE_URL,
            source="arxiv", throttler=throttler, params=params,
        )
        xml_text = resp.text
        cache.set(key, xml_text, ttl_sec=3600)
    return arxiv_atom_to_works(xml_text)
