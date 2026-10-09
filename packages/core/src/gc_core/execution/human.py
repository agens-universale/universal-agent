"""Serializable human requests; no frontend or waiting implementation."""

from dataclasses import dataclass, field
from typing import Awaitable, Callable


class HumanRequestInterrupted(Exception):
    """Input was interrupted; this is not a human permission decision."""


@dataclass
class HumanRequest:
    kind: str
    question: str
    title: str = ""
    fields: list[list[str]] = field(default_factory=list)
    decision_key: str | None = None
    schema_version: int = 1


HumanRequester = Callable[[HumanRequest], Awaitable[str]]
