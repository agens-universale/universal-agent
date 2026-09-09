"""Yaml section ``strip_tags_projection:`` — persist ризонинга в messages + extra-теги derived."""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)

# Имя тега: без пробелов и ``/``; обёртки ``<>`` снимаются до проверки.
_TAG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class StripTagsProjectionConfig(BaseModel):
    """``tags`` — extra-теги derived-сторов. ``strip_think`` — только канон messages.

    ``strip_think: true`` (дефолт) — не класть ``reasoning_content`` в messages.
    ``strip_think: false`` — сохранить поле как пришло; исходящий трейс отдаёт
    канон as-is. Intent / WM / саммари / digest ризонинг режут всегда.
    """

    model_config = ConfigDict(extra="forbid")

    strip_think: bool = True
    tags: list[str] = Field(default_factory=list)


def normalize_strip_tags_projection_config(cfg: StripTagsProjectionConfig) -> None:
    """warn+drop invalid; снять ``<>``; dedupe case-insensitive; ``think`` валиден."""
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
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    cfg.tags = cleaned


def _section_from_config(config: object) -> dict | None:
    if isinstance(config, dict):
        section = config.get("strip_tags_projection")
        if isinstance(section, dict):
            return section
    return None


def projection_tags_from_config(config: object) -> tuple[str, ...]:
    """Extra-теги прозы. Persist-флаг ``strip_think`` сюда не входит."""
    section = _section_from_config(config)
    if section is None:
        return ()
    raw_tags = section.get("tags") or ()
    if isinstance(raw_tags, (list, tuple)):
        return tuple(str(item) for item in raw_tags)
    return ()


def persist_reasoning_content_from_config(config: object) -> bool:
    """Класть ли ``reasoning_content`` на канонический item в ``state.messages``.

    Единственный потребитель — ``get_output_list`` / вход в канон.
    YAML ``strip_think: false`` = persist True.
    """
    section = _section_from_config(config)
    if section is None:
        return False
    return not bool(section.get("strip_think", True))
