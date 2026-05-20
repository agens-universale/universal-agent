from __future__ import annotations

from typing import Any

import httpx

from science_mcp.cache import DiskCache, hash_key
from science_mcp.config import Config
from science_mcp.http_client import request_with_retry
from science_mcp.models import WorkRecord
from science_mcp.normalize import pubmed_xml_to_works
from science_mcp.throttle import Throttler

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


def _key(config: Config) -> dict[str, str]:
    return {"api_key": config.ncbi_api_key} if config.ncbi_api_key else {}


async def esearch_ids(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    db: str,
    term: str,
    retmax: int,
    retstart: int,
) -> list[str]:
    params: dict[str, Any] = {
        "db": db, "term": term, "retmode": "json",
        "retmax": retmax, "retstart": retstart, **_key(config),
    }
    key = hash_key("ncbi", ESEARCH + ":esearch", params)
    cached = cache.get(key)
    if cached is not None:
        return cached
    resp = await request_with_retry(
        client, "GET", ESEARCH,
        source="ncbi", throttler=throttler, params=params,
    )
    ids = resp.json().get("esearchresult", {}).get("idlist", [])
    cache.set(key, ids, ttl_sec=3600)
    return ids


async def efetch_works(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    db: str,
    ids: list[str],
) -> list[WorkRecord]:
    if not ids:
        return []
    params: dict[str, Any] = {
        "db": db, "id": ",".join(ids), "retmode": "xml", **_key(config),
    }
    key = hash_key("ncbi", EFETCH + ":efetch", params)
    cached = cache.get(key)
    if cached is not None:
        return pubmed_xml_to_works(cached)
    resp = await request_with_retry(
        client, "GET", EFETCH,
        source="ncbi", throttler=throttler, params=params,
    )
    xml_text = resp.text
    cache.set(key, xml_text, ttl_sec=86400)
    return pubmed_xml_to_works(xml_text)


async def esummary(
    client: httpx.AsyncClient,
    config: Config,
    throttler: Throttler,
    cache: DiskCache,
    *,
    db: str,
    ids: list[str],
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "db": db, "id": ",".join(ids), "retmode": "json", **_key(config),
    }
    key = hash_key("ncbi", ESUMMARY + ":esummary", params)
    cached = cache.get(key)
    if cached is not None:
        return cached
    resp = await request_with_retry(
        client, "GET", ESUMMARY,
        source="ncbi", throttler=throttler, params=params,
    )
    data = resp.json()
    cache.set(key, data, ttl_sec=86400)
    return data


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
    ids = await esearch_ids(
        client, config, throttler, cache,
        db="pubmed", term=query, retmax=limit, retstart=offset,
    )
    return await efetch_works(client, config, throttler, cache, db="pubmed", ids=ids)
