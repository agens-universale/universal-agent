from __future__ import annotations

from typing import Any

from science_mcp.config import Config

SOURCES_META = [
    {"id": "openalex", "description": "OpenAlex /works", "rate_limit": "10 req/s", "key": None},
    {"id": "crossref", "description": "CrossRef /works", "rate_limit": "30 req/s", "key": None},
    {"id": "ncbi", "description": "PubMed + PMC (E-utilities, shared bucket)",
     "rate_limit": "10 req/s with key, 3 req/s without", "key": "ncbi"},
    {"id": "arxiv", "description": "arXiv Atom API", "rate_limit": "1 req / 3s", "key": None},
    {"id": "semantic_scholar", "description": "Semantic Scholar graph",
     "rate_limit": "key required (no-key returns 429)", "key": "semantic_scholar"},
    {"id": "dblp", "description": "DBLP publication search", "rate_limit": "1 req/s", "key": None},
    {"id": "core", "description": "CORE OA search", "rate_limit": "1 req / 2s", "key": "core"},
    {"id": "opencitations", "description": "OpenCitations Index v1",
     "rate_limit": "3 req/s", "key": None},
]


def run(config: Config) -> dict[str, Any]:
    keys = config.keys_configured()
    sources = []
    for s in SOURCES_META:
        entry: dict[str, Any] = {
            "id": s["id"],
            "description": s["description"],
            "rate_limit": s["rate_limit"],
        }
        if s["key"]:
            entry["key_configured"] = keys.get(s["key"], False)
        sources.append(entry)
    return {
        "ok": True,
        "sources": sources,
        "mailto_configured": config.mailto_is_real(),
    }
