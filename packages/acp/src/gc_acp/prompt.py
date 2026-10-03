"""Склейка content-блоков ACP в один текст для хода ядра."""

from __future__ import annotations

from typing import Any


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
    raise PromptRejected()


def _required_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    raise PromptRejected()
