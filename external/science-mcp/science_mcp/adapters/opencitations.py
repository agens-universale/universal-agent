"""OpenCitations adapter. NOTE: legacy domain opencitations.net/index/coci/api/v1/
issues a 308 to api.opencitations.net/index/v1/ — use the new domain directly."""
from __future__ import annotations

from typing import Any

import httpx

from science_mcp.cache import DiskCache, hash_key
from science_mcp.config import Config
from science_mcp.http_client import request_with_retry
from science_mcp.throttle import Throttler

COUNT_URL = "https://api.opencitations.net/index/v1/citation-count/{doi}"
CITATIONS_URL = "https://api.opencitations.net/index/v1/citations/{doi}"


async def citation_count(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    doi: str,
) -> int:
    url = COUNT_URL.format(doi=doi)
    key = hash_key("opencitations", url, {})
    cached = cache.get(key)
    if cached is not None:
        return cached
    resp = await request_with_retry(
        client, "GET", url,
        source="opencitations", throttler=throttler,
    )
    data = resp.json()
    count = int(data[0]["count"]) if data and "count" in data[0] else 0
    cache.set(key, count, ttl_sec=86400)
    return count


async def citations_list(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    doi: str,
    limit: int,
) -> list[dict[str, Any]]:
    url = CITATIONS_URL.format(doi=doi)
    key = hash_key("opencitations", url, {})
    cached = cache.get(key)
    if cached is None:
        resp = await request_with_retry(
            client, "GET", url,
            source="opencitations", throttler=throttler, timeout=30.0,
        )
        cached = resp.json()
        cache.set(key, cached, ttl_sec=86400)
    return list(cached[:limit])
