"""Проекции каноничных сообщений для sink'ов (view, без мутации state).

Два класса persist (спека 08):

- **A. Canonical thread** (``state.messages``, jsonl) — persist dirty; ``project_*``
  на **use** (format / serialization / extract).
- **B. Derived stores** (intent, WM, project_memory, summary XML) — persist clean
  на **write intake**; read/embed as-is.

Профили:

- ``for_model`` — outbound API, volume/summary text, extract, derived intake:
  стек = один projector с effective tags (``think`` + ``strip_tags_projection.tags``).
- ``for_log`` / ``for_ui`` / ``for_export`` — не читают секцию в 08.

Entrypoint'ы: ``project_text_for_model``, ``project_assistant_text``,
``configure_for_model_tags``.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Sequence

from message_projection.pipeline import MessageProjectionPipeline
from message_projection.projectors.strip_tagged_blocks import (
    strip_tagged_blocks,
    strip_think_blocks,
)
from message_projection.types import Projector

__all__ = [
    "MessageProjectionPipeline",
    "Projector",
    "configure_for_model_tags",
    "default_for_model_pipeline",
    "project_assistant_text",
    "project_text_for_model",
    "strip_tagged_blocks",
    "strip_think_blocks",
]

# Extra tags from config (think always prepended in effective list).
_configured_extra_tags: tuple[str, ...] = ()


def _effective_for_model_tags() -> tuple[str, ...]:
    """``think`` first, then configured extras (case-insensitive dedupe)."""
    seen = {"think"}
    out: list[str] = ["think"]
    for tag in _configured_extra_tags:
        key = str(tag).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(str(tag))
    return tuple(out)


def _strip_for_model(text: str) -> str:
    return strip_tagged_blocks(text, _effective_for_model_tags())


def default_for_model_pipeline() -> MessageProjectionPipeline:
    """Default ``for_model`` stack: strip effective tags (think + configured)."""
    return MessageProjectionPipeline([_strip_for_model])


_DEFAULT_FOR_MODEL = default_for_model_pipeline()


def configure_for_model_tags(extra_tags: Sequence[str]) -> None:
    """Пересобрать default pipeline: ``think`` + ``extra_tags`` (без hot-path yaml)."""
    global _configured_extra_tags, _DEFAULT_FOR_MODEL
    cleaned: list[str] = []
    seen: set[str] = set()
    for tag in extra_tags or ():
        name = str(tag or "").strip()
        if not name:
            continue
        key = name.lower()
        if key == "think" or key in seen:
            continue
        seen.add(key)
        cleaned.append(name)
    _configured_extra_tags = tuple(cleaned)
    _DEFAULT_FOR_MODEL = default_for_model_pipeline()


def project_text_for_model(
    text: str,
    *,
    pipeline: Optional[MessageProjectionPipeline] = None,
) -> str:
    """Прогнать plain text через ``for_model`` pipeline (effective strip tags)."""
    active = pipeline if pipeline is not None else _DEFAULT_FOR_MODEL
    if not isinstance(text, str):
        text = ""
    return active.project(text)


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
    pipeline: Optional[MessageProjectionPipeline] = None,
) -> str:
    """``str | list[{type,text}] | …`` → plain text → ``project_text_for_model``."""
    return project_text_for_model(_content_to_plain_text(content), pipeline=pipeline)
