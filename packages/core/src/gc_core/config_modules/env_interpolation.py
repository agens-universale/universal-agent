"""Резолв ${ENV:VAR} и ${FILE:path} в строках конфига.

Применяется к file-конфигу (после merge, до валидации) и к CLI-override
значениям. Отсутствующий источник → пустая строка + warning с именем/путём
(никогда с содержимым).
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

_logger = logging.getLogger(__name__)

_ENV_MARKER = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")
_FILE_MARKER = re.compile(r"\$\{FILE:([^}]+)\}")
_MAX_FILE_BYTES = 65536  # креды маленькие; защита от указания на большой файл


def _warn_if_lax_permissions(path: str, path_raw: str) -> None:
    """POSIX: файл с кредом доступен group/other → warning (аналогично ssh)."""
    if os.name != "posix":
        return
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if mode & 0o077:
        _logger.warning(
            "credential file is group/world accessible, consider chmod 600: %s",
            path_raw,
        )


def _read_secret_file(path_raw: str, cache: dict[str, str]) -> str:
    """Прочитать файл-кред; ошибка → "" + один warning на уникальный путь."""
    if path_raw in cache:
        return cache[path_raw]
    path = os.path.expanduser(path_raw)

    # Проверка размера отдельно с собственным warning
    try:
        if os.path.getsize(path) > _MAX_FILE_BYTES:
            _logger.warning(
                "credential file too large (> %d bytes), substituted empty string: %s",
                _MAX_FILE_BYTES,
                path_raw,
            )
            cache[path_raw] = ""
            return ""
    except OSError:
        # Файл не существует или иная ошибка доступа - попробуем прочитать и залогируем там
        pass

    try:
        _warn_if_lax_permissions(path, path_raw)
        with open(path, encoding="utf-8-sig") as fd:
            value = fd.read().rstrip("\r\n")
    except (OSError, UnicodeDecodeError) as exc:
        _logger.warning(
            "credential file not readable, substituted empty string: %s (%s)",
            path_raw,
            type(exc).__name__,
        )
        value = ""
    cache[path_raw] = value
    return value


def _resolve(obj: Any, missing_env: set[str], file_cache: dict[str, str]) -> Any:
    if isinstance(obj, dict):
        return {k: _resolve(v, missing_env, file_cache) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve(v, missing_env, file_cache) for v in obj]
    if isinstance(obj, str):
        def repl_env(m: "re.Match[str]") -> str:
            var = m.group(1)
            if var in os.environ:
                return os.environ[var]
            missing_env.add(var)
            return ""

        def repl_file(m: "re.Match[str]") -> str:
            return _read_secret_file(m.group(1), file_cache)

        return _FILE_MARKER.sub(repl_file, _ENV_MARKER.sub(repl_env, obj))
    return obj


def resolve_env_placeholders(obj: Any) -> Any:
    """Рекурсивная немутирующая копия с подстановкой ``${ENV:VAR}`` и ``${FILE:path}``.

    Отсутствующий источник → ``""`` + один warning на уникальное имя/путь.
    """
    missing_env: set[str] = set()
    file_cache: dict[str, str] = {}
    resolved = _resolve(obj, missing_env, file_cache)
    for var in sorted(missing_env):
        _logger.warning("env var not set, substituted empty string: %s", var)
    return resolved


def _resolve_env_only(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _resolve_env_only(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_only(v) for v in obj]
    if isinstance(obj, str):
        return _ENV_MARKER.sub(lambda m: os.environ.get(m.group(1), ""), obj)
    return obj


def resolve_env_only(obj: Any) -> Any:
    """Рекурсивная немутирующая копия с подстановкой ТОЛЬКО ``${ENV:VAR}``.

    ``${FILE:path}`` остаётся ссылкой как есть — файловый резолвер сюда не входит
    вообще, ни чтением, ни статом. Для мест, где раскрытое значение нужно
    посчитать на UI-потоке (`tui/editors/model_entry_form.py::_as_validator_sees`):
    там, где `resolve_env_placeholders` читал бы файл-кред с диска синхронно на
    каждое нажатие клавиши, эта функция гарантированно не тронет диск.

    Без warning вовсе (не только без дедупа): вызывающая сторона такого места сама
    называет причину человеку текстом на экране, и лог на каждый набранный символ
    был бы шумом, а не сигналом."""
    return _resolve_env_only(obj)


__all__ = ["resolve_env_only", "resolve_env_placeholders"]
