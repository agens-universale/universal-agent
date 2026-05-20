from __future__ import annotations

from typing import Any, Optional

import httpx

from science_mcp.cache import DiskCache, hash_key
from science_mcp.config import Config
from science_mcp.errors import ScienceMCPError
from science_mcp.http_client import request_with_retry
from science_mcp.models import WorkRecord
from science_mcp.normalize import crossref_to_work
from science_mcp.throttle import Throttler

BASE_URL = "https://api.crossref.org/works"


async def search(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    query: str,
    limit: int,
    offset: int,
    extra_filter: Optional[str] = None,
) -> list[WorkRecord]:
    params: dict[str, Any] = {"query": query, "rows": limit, "offset": offset}
    if extra_filter:
        params["filter"] = extra_filter
    key = hash_key("crossref", BASE_URL, params)
    cached = cache.get(key)
    if cached is not None:
        items = cached
    else:
        resp = await request_with_retry(
            client, "GET", BASE_URL,
            source="crossref", throttler=throttler, params=params,
        )
        items = resp.json().get("message", {}).get("items", [])
        cache.set(key, items, ttl_sec=3600)
    return [crossref_to_work(it) for it in items]


async def lookup_doi(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    doi: str,
) -> Optional[WorkRecord]:
    url = f"{BASE_URL}/{doi}"
    key = hash_key("crossref", url, {})
    cached = cache.get(key)
    if cached is not None:
        return crossref_to_work(cached)
    try:
        resp = await request_with_retry(
            client, "GET", url,
            source="crossref", throttler=throttler,
        )
    except ScienceMCPError:
        return None
    item = resp.json().get("message")
    if not item:
        return None
    cache.set(key, item, ttl_sec=86400)
    return crossref_to_work(item)
