"""Разбор сырых аргументов вызова тула в словарь — общий для заголовка
субагента и текста `Think`."""

from __future__ import annotations

import json
from typing import Any


def call_arguments(arguments: Any) -> dict:
    """Аргументы вызова словарём; JSON-строка разбирается, битое и не
    словарь — пустой словарь."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return {}
    return arguments if isinstance(arguments, dict) else {}
