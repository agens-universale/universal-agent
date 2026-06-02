from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class Author(BaseModel):
    name: str


class WorkRecord(BaseModel):
    source: str
    id: str
    title: str
    authors: list[Author] = Field(default_factory=list)
    year: Optional[int] = None
    abstract: Optional[str] = None
    doi: Optional[str] = None
    pmid: Optional[str] = None
    pmcid: Optional[str] = None
    arxiv_id: Optional[str] = None
    url: Optional[str] = None
    citation_count: Optional[int] = None
    is_open_access: Optional[bool] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ToolResponse(BaseModel):
    ok: Literal[True] = True
    query: Optional[str] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    has_more: Optional[bool] = None
    sources_queried: list[str] = Field(default_factory=list)
    results: list[WorkRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


ErrorCode = Literal[
    "rate_limited",
    "not_found",
    "invalid_argument",
    "upstream_error",
    "timeout",
    "unauthorized",
    "response_too_large",
]


class ToolError(BaseModel):
    ok: Literal[False] = False
    error: ErrorCode
    source: Optional[str] = None
    message: str
    retry_after_sec: Optional[int] = None
