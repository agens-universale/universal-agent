"""Проекции каноничных сообщений для sink'ов (view, без мутации state).

Профили (v1 внедрён только ``for_model``):

- ``for_model`` — outbound API, volume/summary text, extract last assistant:
  стек v1 = ``[strip_think]``. Later: mask_secrets / PII в тот же стек.
- ``for_log`` — не внедряем; см. ``security.log_redaction``.
- ``for_ui`` / ``for_export`` — не внедряем; TUI/export as-is.

Entrypoint'ы v1: ``project_text_for_model``, ``project_assistant_text``.
Отдельные ``project_*_for_log`` не плодить, пока нет второго стека.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from message_projection.pipeline import MessageProjectionPipeline
from message_projection.projectors.strip_think import strip_think_blocks
from message_projection.types import Projector

__all__ = [
    "MessageProjectionPipeline",
    "Projector",
    "default_for_model_pipeline",
    "project_assistant_text",
    "project_text_for_model",
    "strip_think_blocks",
]


def default_for_model_pipeline() -> MessageProjectionPipeline:
    """Default ``for_model`` stack v1: strip inline think blocks."""
    return MessageProjectionPipeline([strip_think_blocks])


_DEFAULT_FOR_MODEL = default_for_model_pipeline()


def project_text_for_model(
    text: str,
    *,
    pipeline: Optional[MessageProjectionPipeline] = None,
) -> str:
    """Прогнать plain text через ``for_model`` pipeline (v1: strip_think)."""
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
