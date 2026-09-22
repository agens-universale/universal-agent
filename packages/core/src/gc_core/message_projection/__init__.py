"""Проекции каноничных сообщений для sink'ов (view, без мутации state).

Два класса persist (спека 08):

- **A. Canonical thread** (``state.messages``, jsonl) — persist dirty; ``project_*``
  на **use** (format / serialization / extract).
- **B. Derived stores** (intent, WM, project_memory, summary XML) — persist clean
  на **write intake**; read/embed as-is.

Профили:

- ``for_model`` — volume/summary text, extract, derived intake:
  стек = один projector с итоговым списком тегов (``think`` первым при
  ``strip_think``, затем ``tags``). Identity, только если итоговый список пуст.
- Основной outbound (запрос к модели) отдаёт канон as-is, без этой проекции.
- ``for_log`` / ``for_ui`` / ``for_export`` — не читают секцию в 08.

Entrypoint'ы: ``project_text_for_model``, ``project_assistant_text``,
``effective_for_model_tags``. Параметры ``tags`` / ``strip_think`` передаёт
вызывающий (из ``config_dict``), модуль состояние не хранит.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Sequence

from gc_core.message_projection.pipeline import MessageProjectionPipeline
from gc_core.message_projection.projectors.strip_tagged_blocks import (
    strip_tagged_blocks,
    strip_think_blocks,
)
from gc_core.message_projection.types import Projector

__all__ = [
    "MessageProjectionPipeline",
    "Projector",
    "effective_for_model_tags",
    "for_model_pipeline",
    "project_assistant_text",
    "project_text_for_model",
    "strip_tagged_blocks",
    "strip_think_blocks",
]


def effective_for_model_tags(
    tags: Sequence[str] = (),
    *,
    strip_think: bool = True,
) -> tuple[str, ...]:
    """Итоговый список: ``think`` первым при ``strip_think``, затем остальные ``tags``."""
    out: list[str] = []
    seen: set[str] = set()
    if strip_think:
        out.append("think")
        seen.add("think")
    for tag in tags or ():
        name = str(tag or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return tuple(out)


def for_model_pipeline(
    tags: Sequence[str] = (),
    *,
    strip_think: bool = True,
) -> MessageProjectionPipeline:
    """``for_model``: strip итогового списка; identity, если он пуст."""
    effective = effective_for_model_tags(tags, strip_think=strip_think)
    if not effective:
        return MessageProjectionPipeline([])
    return MessageProjectionPipeline([lambda text, _tags=effective: strip_tagged_blocks(text, _tags)])


def project_text_for_model(
    text: str,
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
    pipeline: Optional[MessageProjectionPipeline] = None,
) -> str:
    """Прогнать plain text через ``for_model`` (итоговый список тегов)."""
    if not isinstance(text, str):
        text = ""
    if pipeline is not None:
        return pipeline.project(text)
    effective = effective_for_model_tags(tags, strip_think=strip_think)
    if not effective:
        return text
    return strip_tagged_blocks(text, effective)


def _content_to_plain_text(content: Any) -> str:
    """Единое извлечение plain text из assistant content (str | list parts | …)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n\n".join(parts)
    if content is None:
        return ""
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    return str(content)


def project_assistant_text(
    content: Any,
    *,
    tags: Sequence[str] = (),
    strip_think: bool = True,
    pipeline: Optional[MessageProjectionPipeline] = None,
) -> str:
    """``str | list[{type,text}] | …`` → plain text → ``project_text_for_model``."""
    return project_text_for_model(
        _content_to_plain_text(content),
        tags=tags,
        strip_think=strip_think,
        pipeline=pipeline,
    )
