"""Yaml section ``strip_tags_projection:`` — доп. теги к always-on ``think``."""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)

# Имя тега: без пробелов и ``/``; обёртки ``<>`` снимаются до проверки.
_TAG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class StripTagsProjectionConfig(BaseModel):
    """Доп. strip-теги для ``for_model`` / derived intake. ``think`` всегда в коде."""

    model_config = ConfigDict(extra="forbid")

    tags: list[str] = Field(default_factory=list)


def normalize_strip_tags_projection_config(cfg: StripTagsProjectionConfig) -> None:
    """warn+drop invalid; снять ``<>``; dedupe case-insensitive; ``think`` — no-op."""
    raw = list(cfg.tags or [])
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            _logger.warning(
                "strip_tags_projection.tags: non-string entry %r dropped", item
            )
            continue
        name = item.strip()
        if name.startswith("<") and name.endswith(">") and len(name) >= 2:
            name = name[1:-1].strip()
        if not name:
            _logger.warning("strip_tags_projection.tags: empty/whitespace tag dropped")
            continue
        if "/" in name or any(ch.isspace() for ch in name):
            _logger.warning(
                "strip_tags_projection.tags: invalid tag %r dropped", item
            )
            continue
        if not _TAG_NAME_RE.match(name):
            _logger.warning(
                "strip_tags_projection.tags: invalid tag %r dropped", item
            )
            continue
        key = name.lower()
        if key == "think":
            # Always-on in code; YAML entry is a no-op.
            continue
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    cfg.tags = cleaned
