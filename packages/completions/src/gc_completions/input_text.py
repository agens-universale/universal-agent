"""Разбор ``messages`` и рабочего каталога хода."""

from __future__ import annotations

import os
from dataclasses import dataclass

from gc_core.message_projection.projectors.strip_tagged_blocks import (
    strip_think_blocks,
)

from gc_completions.errors import ApiError

_INVALID = "invalid_request"
_USER = "user"
_ASSISTANT = "assistant"
_SKIP_ROLES = frozenset({"system", "tool"})


@dataclass(frozen=True)
class IncomingTurn:
    model: str
    prompt: str
    prior: tuple[tuple[str, str], ...]
    cwd_sent: str | None
    stream: bool


def parse_turn(body: object) -> IncomingTurn:
    if not isinstance(body, dict):
        raise ApiError(400, _INVALID, "request body must be an object")
    model = body.get("model")
    if not isinstance(model, str) or model == "":
        raise ApiError(400, _INVALID, "model is required")
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise ApiError(400, _INVALID, "messages is required")
    prompt, prior = _dialog(messages)
    return IncomingTurn(
        model=model,
        prompt=prompt,
        prior=tuple(prior),
        cwd_sent=metadata_cwd(body.get("metadata")),
        stream=body.get("stream") is True,
    )


def metadata_cwd(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    cwd = value.get("cwd")
    if not isinstance(cwd, str) or cwd == "":
        return None
    return cwd


def cwd_for_turn(sent: str | None, launch_root: object) -> str:
    if isinstance(sent, str) and os.path.isabs(sent):
        return os.path.normpath(sent)
    if isinstance(launch_root, str) and os.path.isabs(launch_root):
        return os.path.normpath(launch_root)
    return os.path.normpath(os.getcwd())


def _dialog(messages: list) -> tuple[str, list[tuple[str, str]]]:
    if not messages:
        raise ApiError(400, _INVALID, "messages has no user text")
    prompt = _last_user_text(messages[-1])
    prior: list[tuple[str, str]] = []
    for element in messages[:-1]:
        pair = _prior_pair(element)
        if pair is not None:
            prior.append(pair)
    return prompt, prior


def _last_user_text(element: object) -> str:
    if not isinstance(element, dict) or element.get("role") != _USER:
        raise ApiError(400, _INVALID, "last message must be a user message")
    text = _content_text(element.get("content"))
    if text == "":
        raise ApiError(400, _INVALID, "last message has no text")
    return text


def _prior_pair(element: object) -> tuple[str, str] | None:
    if not isinstance(element, dict):
        return None
    role = element.get("role")
    if role in _SKIP_ROLES or "tool_calls" in element:
        return None
    if role not in (_USER, _ASSISTANT):
        return None
    text = _content_text(element.get("content"))
    if role == _ASSISTANT:
        text = strip_think_blocks(text)
    if text == "":
        return None
    return (str(role), text)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        text = _part_text(part)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _part_text(part: object) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    kind = part.get("type")
    if kind not in (None, "text"):
        return ""
    text = part.get("text")
    if isinstance(text, str):
        return text
    return ""
