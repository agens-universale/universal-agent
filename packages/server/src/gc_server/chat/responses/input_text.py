"""Разбор ``input`` и ``metadata`` запроса хода."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gc_server.chat.cwd import metadata_cwd, same_cwd
from gc_server.chat.errors import ApiError

_INVALID = "invalid_request"


@dataclass(frozen=True)
class IncomingTurn:
    model: str
    text: str
    previous_response_id: str | None
    metadata: dict
    cwd_sent: str | None
    stream: bool


def parse_turn(body: Any) -> IncomingTurn:
    if not isinstance(body, dict):
        raise ApiError(400, _INVALID, "request body must be an object")
    model = body.get("model")
    if not isinstance(model, str) or model == "":
        raise ApiError(400, _INVALID, "model is required")
    if "input" not in body:
        raise ApiError(400, _INVALID, "input is required")
    text = turn_text(body.get("input"))
    if text == "":
        raise ApiError(400, _INVALID, "input has no text")
    previous = _previous(body.get("previous_response_id", None))
    metadata = response_metadata(body.get("metadata"))
    return IncomingTurn(
        model=model,
        text=text,
        previous_response_id=previous,
        metadata=metadata,
        cwd_sent=metadata_cwd(body.get("metadata")),
        stream=body.get("stream") is True,
    )


def turn_text(value: Any) -> str:
    """Текст хода. Из массива берётся только последний элемент."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if not value:
            return ""
        return _element_text(value[-1])
    return ""


def response_metadata(value: Any) -> dict:
    """Копия объекта строковых пар. Иначе пустой объект."""
    if not isinstance(value, dict):
        return {}
    if any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        return {}
    return dict(value)


def cwd_for_continue(sent: str | None, recorded: str) -> str:
    if sent is not None and not same_cwd(sent, recorded):
        raise ApiError(400, _INVALID, "cwd mismatch")
    return recorded


def _previous(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise ApiError(400, _INVALID, "previous_response_id is invalid")
    return value


def _element_text(element: Any) -> str:
    if isinstance(element, str):
        return element
    if not isinstance(element, dict) or not _is_user_message(element):
        return ""
    return _content_text(element.get("content"))


def _is_user_message(element: dict) -> bool:
    if element.get("role") != "user":
        return False
    kind = element.get("type")
    return kind is None or kind == "message"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [_input_text(item) for item in content]
    return "\n".join(part for part in parts if part is not None)


def _input_text(item: Any) -> str | None:
    if not isinstance(item, dict) or item.get("type") != "input_text":
        return None
    text = item.get("text")
    if not isinstance(text, str):
        return None
    return text
