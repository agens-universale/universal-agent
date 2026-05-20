from __future__ import annotations

import json
import re
from typing import Literal

import httpx

from science_mcp.adapters import crossref
from science_mcp.cache import DiskCache, hash_key
from science_mcp.config import Config
from science_mcp.errors import ScienceMCPError
from science_mcp.http_client import request_with_retry
from science_mcp.models import WorkRecord
from science_mcp.throttle import Throttler

DETAILS_URL = "https://api.biorxiv.org/details/{server}/{doi}"
_PREPRINT_DOI_RE = re.compile(r"^10\.1101/\d{4}\.\d{2}\.\d{2}\.\d+")


async def _details_exists(
    client: httpx.AsyncClient,
    throttler: Throttler,
    cache: DiskCache,
    *,
    server: Literal["biorxiv", "medrxiv"],
    doi: str,
) -> bool:
    url = DETAILS_URL.format(server=server, doi=doi)
    key = hash_key("biorxiv", url, {})
    cached = cache.get(key)
    if cached is not None:
        return bool(cached)
    try:
        resp = await request_with_retry(
            client, "GET", url,
            source="biorxiv", throttler=throttler, timeout=10.0,
        )
        data = resp.json()
        ok = bool(data.get("collection"))
    except (ScienceMCPError, json.JSONDecodeError, ValueError, KeyError):
        ok = False
    cache.set(key, ok, ttl_sec=86400)
    return ok


async def search(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    query: str,
    limit: int,
    offset: int,
    server: Literal["biorxiv", "medrxiv"] = "biorxiv",
    fetch_details: bool = False,
) -> list[WorkRecord]:
    candidates = await crossref.search(
        client, config, throttler, cache,
        query=query, limit=limit * 3, offset=offset,
        extra_filter="member:246",
    )
    preprints = [r for r in candidates if r.doi and r.doi.startswith("10.1101/")]
    preprints = [r for r in preprints if r.doi and _PREPRINT_DOI_RE.match(r.doi)]
    if fetch_details:
        verified: list[WorkRecord] = []
        for r in preprints:
            assert r.doi is not None
            if await _details_exists(client, throttler, cache, server=server, doi=r.doi):
                r.source = server
                verified.append(r)
        preprints = verified
    else:
        for r in preprints:
            r.source = server
    return preprints[:limit]
