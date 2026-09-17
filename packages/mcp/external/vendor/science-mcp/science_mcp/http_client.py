from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from science_mcp.config import Config
from science_mcp.errors import ErrorCategory, ScienceMCPError
from science_mcp.throttle import Throttler


def build_client(config: Config, *, timeout: Optional[float] = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout or config.http_timeout_sec,
        follow_redirects=True,
        headers={
            "User-Agent": f"ScienceMCP/0.1 (mailto:{config.mailto})",
            "Accept": "application/json, application/xml;q=0.9, */*;q=0.5",
        },
    )


def raise_for_status(resp: httpx.Response, *, source: str) -> None:
    if resp.status_code < 400:
        return
    retry_after: Optional[int] = None
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            retry_after = int(ra)
        except ValueError:
            retry_after = None
    if resp.status_code == 429:
        raise ScienceMCPError(
            ErrorCategory.RATE_LIMITED, source=source, retry_after_sec=retry_after
        )
    if resp.status_code in (401, 403):
        raise ScienceMCPError(ErrorCategory.UNAUTHORIZED, source=source)
    if resp.status_code >= 500:
        raise ScienceMCPError(
            ErrorCategory.UPSTREAM_ERROR, source=source, detail=f"HTTP {resp.status_code}"
        )
    raise ScienceMCPError(
        ErrorCategory.UPSTREAM_ERROR,
        source=source,
        detail=f"HTTP {resp.status_code}: {resp.text[:200]}",
    )


# Per-provider retry policy. pause_sec: fixed wait on 429 when no Retry-After
# header. max_retries: extra attempts after the first try. retry_5xx: whether
# to retry on 500/502/503. exp_backoff: if True, wait pause_sec * 2**attempt.
RETRY_POLICY: dict[str, dict[str, Any]] = {
    "openalex":         {"pause_sec": 30,  "max_retries": 2, "retry_5xx": True,  "exp_backoff": False},
    "ncbi":             {"pause_sec": 30,  "max_retries": 2, "retry_5xx": True,  "exp_backoff": False},
    "crossref":         {"pause_sec": 30,  "max_retries": 2, "retry_5xx": True,  "exp_backoff": False},
    "arxiv":            {"pause_sec": 90,  "max_retries": 1, "retry_5xx": True,  "exp_backoff": False},
    "semantic_scholar": {"pause_sec": 2,   "max_retries": 3, "retry_5xx": True,  "exp_backoff": True},
    "dblp":             {"pause_sec": 30,  "max_retries": 2, "retry_5xx": True,  "exp_backoff": False},
    "core":             {"pause_sec": 30,  "max_retries": 2, "retry_5xx": True,  "exp_backoff": False},
    "opencitations":    {"pause_sec": 30,  "max_retries": 2, "retry_5xx": True,  "exp_backoff": False},
    "biorxiv":          {"pause_sec": 30,  "max_retries": 1, "retry_5xx": True,  "exp_backoff": False},
}

_DEFAULT_POLICY = {"pause_sec": 30, "max_retries": 2, "retry_5xx": True, "exp_backoff": False}


def _compute_wait(
    resp: httpx.Response, source: str, attempt: int, policy: dict[str, Any]
) -> float:
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return float(int(ra))
        except ValueError:
            pass
    if policy["exp_backoff"]:
        return float(policy["pause_sec"] * (2 ** attempt))
    return float(policy["pause_sec"])


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    source: str,
    throttler: Throttler,
    **kwargs: Any,
) -> httpx.Response:
    """HTTP request with throttler acquisition + retry on 429/5xx.

    Always raises a ScienceMCPError on terminal failure (mapped via raise_for_status).
    Returns the 2xx response on success.
    """
    policy = RETRY_POLICY.get(source, _DEFAULT_POLICY)
    attempts = 0
    while True:
        await throttler.acquire(source)
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TimeoutException as e:
            raise ScienceMCPError(
                ErrorCategory.TIMEOUT, source=source, detail=str(e)
            ) from e
        except httpx.HTTPError as e:
            raise ScienceMCPError(
                ErrorCategory.UPSTREAM_ERROR, source=source, detail=str(e)
            ) from e
        if resp.status_code < 400:
            return resp
        retryable_status = resp.status_code == 429 or (
            policy["retry_5xx"] and resp.status_code in (500, 502, 503)
        )
        if not retryable_status or attempts >= policy["max_retries"]:
            raise_for_status(resp, source=source)
        wait = _compute_wait(resp, source, attempts, policy)
        await asyncio.sleep(wait)
        attempts += 1
