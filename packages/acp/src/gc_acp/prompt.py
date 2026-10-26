"""Склейка content-блоков ACP в один текст для хода ядра."""

from __future__ import annotations

from typing import Any

# Потолок тела одного embedded resource. Маркер обрезки в него не входит.
_RESOURCE_TEXT_MAX_BYTES = 256 * 1024


class PromptRejected(Exception):
    """Промпт нельзя отдать модели: -32602, ход не стартует."""


def flatten_prompt(prompt: Any) -> str:
    """Склеить блоки через перевод строки. Пустой текст — отказ."""
    if not isinstance(prompt, list) or not prompt:
        raise PromptRejected()
    lines = [_block_line(block) for block in prompt]
    text = "\n".join(lines)
    if text.strip() == "":
        raise PromptRejected()
    return text


def _block_line(block: Any) -> str:
    if not isinstance(block, dict):
        raise PromptRejected()
    kind = block.get("type")
    if kind == "text":
        return _required_str(block.get("text"))
    if kind == "resource_link":
        name = _required_str(block.get("name"))
        uri = _required_str(block.get("uri"))
        return f"{name} ({uri})"
    if kind == "resource":
        return _resource_line(block.get("resource"))
    if kind == "image":
        raise PromptRejected("image not supported")
    if kind == "audio":
        raise PromptRejected("audio not supported")
    raise PromptRejected()


def _resource_line(resource: Any) -> str:
    if not isinstance(resource, dict):
        raise PromptRejected()
    text = resource.get("text")
    if isinstance(text, str) and text.strip() != "":
        uri = resource.get("uri")
        if not isinstance(uri, str) or uri.strip() == "":
            raise PromptRejected()
        return f"{uri}\n{_clip_resource_text(text)}"
    blob = resource.get("blob")
    if isinstance(blob, str) and blob != "":
        raise PromptRejected("embedded blob not supported")
    raise PromptRejected()


def _clip_resource_text(text: str) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= _RESOURCE_TEXT_MAX_BYTES:
        return text
    end = _RESOURCE_TEXT_MAX_BYTES
    while end > 0:
        try:
            clipped = raw[:end].decode("utf-8")
        except UnicodeDecodeError:
            end -= 1
            continue
        return f"{clipped}\n[truncated]"
    raise PromptRejected()


def _required_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    raise PromptRejected()
