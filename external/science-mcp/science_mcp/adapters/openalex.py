from __future__ import annotations

from typing import Any, Optional

import httpx

from science_mcp.cache import DiskCache, hash_key
from science_mcp.config import Config
from science_mcp.errors import ScienceMCPError
from science_mcp.http_client import request_with_retry
from science_mcp.models import WorkRecord
from science_mcp.normalize import openalex_to_work
from science_mcp.throttle import Throttler

BASE_URL = "https://api.openalex.org/works"


async def search(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    query: str,
    limit: int,
    offset: int,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
) -> list[WorkRecord]:
    per_page = max(limit, 1)
    page = (offset // per_page) + 1
    params: dict[str, Any] = {
        "search": query,
        "per_page": per_page,
        "page": page,
        "mailto": config.mailto,
    }
    filters: list[str] = []
    if year_from:
        filters.append(f"from_publication_date:{year_from}-01-01")
    if year_to:
        filters.append(f"to_publication_date:{year_to}-12-31")
    if filters:
        params["filter"] = ",".join(filters)

    key = hash_key("openalex", BASE_URL, params)
    cached = cache.get(key)
    if cached is not None:
        raw_results = cached
    else:
        resp = await request_with_retry(
            client, "GET", BASE_URL,
            source="openalex", throttler=throttler, params=params,
        )
        raw_results = resp.json().get("results", [])
        cache.set(key, raw_results, ttl_sec=3600)
    return [openalex_to_work(r) for r in raw_results]


async def _lookup(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    path: str,
) -> Optional[WorkRecord]:
    url = f"{BASE_URL}/{path}"
    params = {"mailto": config.mailto}
    key = hash_key("openalex", url, params)
    cached = cache.get(key)
    if cached is not None:
        return openalex_to_work(cached)
    try:
        resp = await request_with_retry(
            client, "GET", url,
            source="openalex", throttler=throttler, params=params,
        )
    except ScienceMCPError:
        return None
    data = resp.json()
    cache.set(key, data, ttl_sec=86400)
    return openalex_to_work(data)


async def lookup_doi(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    doi: str,
) -> Optional[WorkRecord]:
    return await _lookup(client, config, throttler, cache, path=f"doi:{doi}")


async def lookup_work_id(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    work_id: str,
) -> Optional[WorkRecord]:
    return await _lookup(client, config, throttler, cache, path=work_id)
