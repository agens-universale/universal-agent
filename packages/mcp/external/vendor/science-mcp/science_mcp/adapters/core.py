from __future__ import annotations

from typing import Any

import httpx

from science_mcp.cache import DiskCache, hash_key
from science_mcp.config import Config
from science_mcp.http_client import request_with_retry
from science_mcp.models import WorkRecord
from science_mcp.normalize import core_to_work
from science_mcp.throttle import Throttler

# Trailing slash required — without it CORE returns 301 HTML refresh.
BASE_URL = "https://api.core.ac.uk/v3/search/works/"


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
    params: dict[str, Any] = {"q": query, "limit": limit, "offset": offset}
    headers: dict[str, str] = {}
    if config.core_api_key:
        headers["Authorization"] = f"Bearer {config.core_api_key}"
    key = hash_key("core", BASE_URL, params)
    cached = cache.get(key)
    if cached is not None:
        results = cached
    else:
        resp = await request_with_retry(
            client, "GET", BASE_URL,
            source="core", throttler=throttler, params=params, headers=headers,
        )
        results = resp.json().get("results") or []
        cache.set(key, results, ttl_sec=3600)
    return [core_to_work(r) for r in results]
