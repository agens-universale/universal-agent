"""Резолв ${ENV:VAR} в строках конфига из переменных окружения.

Применяется к file-конфигу (после merge, до валидации) и к CLI-override значениям.
Отсутствующая переменная → пустая строка + warning с именем (не значением).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

_logger = logging.getLogger(__name__)

_ENV_MARKER = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve(obj: Any, missing: set[str]) -> Any:
    if isinstance(obj, dict):
        return {k: _resolve(v, missing) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve(v, missing) for v in obj]
    if isinstance(obj, str):
        def repl(m: "re.Match[str]") -> str:
            var = m.group(1)
            if var in os.environ:
                return os.environ[var]
            missing.add(var)
            return ""
        return _ENV_MARKER.sub(repl, obj)
    return obj


def resolve_env_placeholders(obj: Any) -> Any:
    """Рекурсивная немутирующая копия с подстановкой ``${ENV:VAR}``.

    Отсутствующая переменная → ``""`` + один warning на уникальное имя.
    """
    missing: set[str] = set()
    resolved = _resolve(obj, missing)
    for var in sorted(missing):
        _logger.warning("env var not set, substituted empty string: %s", var)
    return resolved


__all__ = ["resolve_env_placeholders"]
