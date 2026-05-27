from __future__ import annotations

from typing import Any, Optional

import httpx

from science_mcp.cache import DiskCache, hash_key
from science_mcp.config import Config
from science_mcp.errors import ErrorCategory, ScienceMCPError
from science_mcp.http_client import request_with_retry
from science_mcp.models import WorkRecord
from science_mcp.normalize import s2_to_work
from science_mcp.throttle import Throttler

BASE_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
LOOKUP_URL = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
DEFAULT_FIELDS = "title,authors,year,abstract,url,citationCount,externalIds"


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
    if not config.semantic_scholar_api_key:
        raise ScienceMCPError(ErrorCategory.UNAUTHORIZED, source="semantic_scholar")
    params: dict[str, Any] = {
        "query": query, "limit": limit, "offset": offset, "fields": DEFAULT_FIELDS,
    }
    headers = {"x-api-key": config.semantic_scholar_api_key}
    key = hash_key("semantic_scholar", BASE_URL, params)
    cached = cache.get(key)
    if cached is not None:
        data = cached
    else:
        resp = await request_with_retry(
            client, "GET", BASE_URL,
            source="semantic_scholar", throttler=throttler,
            params=params, headers=headers,
        )
        data = resp.json().get("data") or []
        cache.set(key, data, ttl_sec=3600)
    return [s2_to_work(r) for r in data]


async def lookup_doi(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    doi: str,
) -> Optional[WorkRecord]:
    if not config.semantic_scholar_api_key:
        return None
    url = LOOKUP_URL.format(doi=doi)
    params = {"fields": DEFAULT_FIELDS}
    headers = {"x-api-key": config.semantic_scholar_api_key}
    key = hash_key("semantic_scholar", url, params)
    cached = cache.get(key)
    if cached is not None:
        return s2_to_work(cached)
    try:
        resp = await request_with_retry(
            client, "GET", url,
            source="semantic_scholar", throttler=throttler,
            params=params, headers=headers,
        )
    except ScienceMCPError:
        return None
    data = resp.json()
    if not data:
        return None
    cache.set(key, data, ttl_sec=86400)
    return s2_to_work(data)
