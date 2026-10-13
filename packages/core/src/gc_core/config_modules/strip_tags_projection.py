"""Yaml section ``strip_tags_projection:`` — persist ризонинга в messages + extra-теги derived."""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, ConfigDict, Field

from gc_core.config_modules.editing import editable

_logger = logging.getLogger(__name__)

# Имя тега: без пробелов и ``/``; обёртки ``<>`` снимаются до проверки.
_TAG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class StripTagsProjectionConfig(BaseModel):
    """``tags`` — extra-теги для саммари/intent/digest. ``strip_think`` — история.

    ``strip_think: false`` (дефолт) — копировать скрытое поле мысли в историю.
    ``strip_think: true`` — не копировать. Salvage ``<think>`` — только если
    кроме ризонинга ничего нет (нет прозы и нет тулов). Смешанный ход
    (think + проза) кладёт в историю только прозу.
    История саммари и патч памяти оставляют ``<think>`` в тексте ответа
    и дописывают клип ``reasoning_content``. Хвост ``<last_dialog_steps>``
    для основной модели — без этих тегов и без поля reasoning.
    Intent, судьи и ответ aux-модели по-прежнему без этих тегов.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    strip_think: bool = False
    tags: list[str] = Field(default_factory=list)


def normalize_strip_tags_projection_config(
    cfg: StripTagsProjectionConfig,
) -> StripTagsProjectionConfig:
    """warn+drop invalid; снять ``<>``; dedupe case-insensitive; ``think`` валиден."""
    cfg = editable(cfg)
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
    return cfg.result()


def _section_from_config(settings: object) -> StripTagsProjectionConfig:
    """Секция ``strip_tags_projection`` из всего, что принимает ``settings_of``."""
    # Ленивый импорт: settings → root → этот модуль.
    from gc_core.settings import settings_of

    return settings_of(settings).strip_tags_projection


def projection_tags_from_config(settings: object) -> tuple[str, ...]:
    """Extra-теги прозы. Persist-флаг ``strip_think`` сюда не входит."""
    return tuple(_section_from_config(settings).tags)


def persist_reasoning_content_from_config(settings: object) -> bool:
    """Класть ли ``reasoning_content`` на канонический item в ``state.messages``.

    Глобальный флаг. Дефолт ``strip_think: false`` — поле копируется.
    ``true`` в конфиге вырезание включает. Алиас со своим ``strip_think``
    перекрывает его через ``persist_reasoning_content_for_model``. Служебные
    вызовы этот флаг не читают. История саммари и патч памяти оставляют
    ``<think>`` в тексте ответа и дописывают клип ``reasoning_content``.
    Хвост ``<last_dialog_steps>`` — без этих тегов и без поля reasoning.
    Intent, судьи, finish_gate и ответ aux-модели идут без этих тегов.
    """
    return not _section_from_config(settings).strip_think


def persist_reasoning_content_for_model(
    resolved: object, settings: object
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
    return persist_reasoning_content_from_config(settings)


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
