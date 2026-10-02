"""Yaml section ``strip_tags_projection:`` — persist ризонинга в messages + extra-теги derived."""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)

# Имя тега: без пробелов и ``/``; обёртки ``<>`` снимаются до проверки.
_TAG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class StripTagsProjectionConfig(BaseModel):
    """``tags`` — extra-теги для саммари/intent/digest. ``strip_think`` — история.

    ``strip_think: false`` (дефолт) — копировать скрытое поле мысли в историю.
    ``strip_think: true`` — не копировать. Salvage ``<think>`` — только если
    кроме ризонинга ничего нет (нет прозы и нет тулов). Смешанный ход
    (think + проза) кладёт в историю только прозу.
    Из саммари, intent, судей и памяти ``<think>`` вырезается всегда.
    """

    model_config = ConfigDict(extra="forbid")

    strip_think: bool = False
    tags: list[str] = Field(default_factory=list)


def normalize_strip_tags_projection_config(
    cfg: StripTagsProjectionConfig,
) -> None:
    """warn+drop invalid; снять ``<>``; dedupe case-insensitive; ``think`` валиден."""
    raw = list(cfg.tags or [])
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = _clean_tag_name(item)
        if name is None:
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

    Глобальный флаг. Дефолт ``strip_think: false`` — поле копируется.
    ``true`` в конфиге вырезание включает. Алиас со своим ``strip_think``
    перекрывает его через ``persist_reasoning_content_for_model``. Служебные
    вызовы этот флаг не читают: summary, судьи, finish_gate и intent режут
    мысль всегда.
    """
    section = _section_from_config(config)
    if section is None:
        return True
    return not bool(section.get("strip_think", False))


def persist_reasoning_content_for_model(
    resolved: object, config: object
) -> bool:
    """Persist мысли хода этой модели.

    ``strip_think`` алиаса, если он задан булевым. Ключа нет — глобальный
    ``strip_tags_projection.strip_think``. ``true`` не копирует поле в историю,
    ``false`` копирует.
    """
    flag = (
        getattr(resolved, "strip_think", None)
        if resolved is not None
        else None
    )
    if isinstance(flag, bool):
        return not flag
    return persist_reasoning_content_from_config(config)


def _clean_tag_name(item: object) -> str | None:
    """Очищенное имя тега или ``None`` (с warning), если запись невалидна."""
    if not isinstance(item, str):
        _logger.warning(
            "strip_tags_projection.tags: non-string entry %r dropped", item
        )
        return None
    name = item.strip()
    if name.startswith("<") and name.endswith(">") and len(name) >= 2:
        name = name[1:-1].strip()
    if not name:
        _logger.warning(
            "strip_tags_projection.tags: empty/whitespace tag dropped"
        )
        return None
    if _is_invalid_tag_name(name):
        _logger.warning(
            "strip_tags_projection.tags: invalid tag %r dropped", item
        )
        return None
    return name


def _is_invalid_tag_name(name: str) -> bool:
    if "/" in name or any(ch.isspace() for ch in name):
        return True
    return not _TAG_NAME_RE.match(name)
