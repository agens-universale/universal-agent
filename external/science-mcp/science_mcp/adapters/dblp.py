from __future__ import annotations

from typing import Any

import httpx

from science_mcp.cache import DiskCache, hash_key
from science_mcp.config import Config
from science_mcp.http_client import request_with_retry
from science_mcp.models import WorkRecord
from science_mcp.normalize import dblp_hit_to_work
from science_mcp.throttle import Throttler

BASE_URL = "https://dblp.org/search/publ/api"


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
    params: dict[str, Any] = {"q": query, "format": "json", "h": limit, "f": offset}
    key = hash_key("dblp", BASE_URL, params)
    cached = cache.get(key)
    if cached is not None:
        hits = cached
    else:
        resp = await request_with_retry(
            client, "GET", BASE_URL,
            source="dblp", throttler=throttler, params=params, timeout=25.0,
        )
        hits = ((resp.json().get("result") or {}).get("hits") or {}).get("hit") or []
        cache.set(key, hits, ttl_sec=3600)
    return [dblp_hit_to_work(h) for h in hits]
