"""Настройки из командной строки — один контракт у gc-cli, gc-acp и gc-chat.

Настройка задаётся путём через точку (``--agent.max_steps 50``) или
объявленным флагом (``--model``, ``--completions_url``, ``--level``). Имя
проверяет ``apply_cli_overrides``: незнакомый путь — ``UnknownSettingError``
с подсказкой.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, Iterable, List

import yaml

#: Объявленные флаги-настройки → путь в схеме. Легаси-форс
#: (``config.cli_forces_legacy_model``) узнаёт ``--model`` и
#: ``--completions_url`` по слою ``cli`` их путей.
SETTING_FLAGS = {
    "model": "agent.model",
    "completions_url": "backend.completions_url",
    "level": "level",
}

LEGACY_FORCE_NOTICE = "--model: уровни отключены на этот запуск (легаси-форс)"


def coerce_cli_value(raw: str) -> Any:
    """Строка CLI → тип через ``yaml.safe_load`` (числа, bool, списки,
    словари); при неудаче или ``None`` — исходная строка."""
    try:
        value = yaml.safe_load(raw)
    except Exception:
        return raw
    return raw if value is None else value


def parse_override_args(tokens: List[str]) -> Dict[str, Any]:
    """``--путь value`` / ``--путь=value`` / ``--флаг`` → ``{путь: значение}``.

    Позиционные токены пропускаются.
    """
    overrides: Dict[str, Any] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("--"):
            i += 1
            continue
        key_part = token[2:]
        value: Any = True
        if "=" in key_part:
            key, raw_val = key_part.split("=", 1)
            value = coerce_cli_value(raw_val)
        else:
            key = key_part
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                value = coerce_cli_value(tokens[i + 1])
                i += 1
        if key:
            overrides[key] = value
        i += 1
    return overrides


def add_setting_flags(parser: argparse.ArgumentParser) -> None:
    """``--model``, ``--completions_url``, ``--level`` — как у gc-cli."""
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "модель на этот запуск (agent.model); при непустом реестре "
            "моделей — легаси-форс"
        ),
    )
    parser.add_argument(
        "--completions_url",
        "--completions-url",
        default=None,
        dest="completions_url",
        help="адрес chat/completions на этот запуск (backend.completions_url)",
    )
    parser.add_argument(
        "--level",
        choices=["light", "default", "max"],
        default=None,
        help="активный уровень модели на этот запуск",
    )


def setting_flag_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    """Заданные объявленные флаги-настройки → ``{путь: значение}``."""
    return {
        path: getattr(args, flag)
        for flag, path in SETTING_FLAGS.items()
        if getattr(args, flag, None) is not None
    }


def split_setting_paths(
    overrides: Dict[str, Any],
) -> tuple[Dict[str, Any], List[str]]:
    """Пути настроек (с точкой) и прочие имена.

    Точка входа, которую запускает чужой клиент (gc-acp), пропускает имена
    без точки: клиенты дописывают свои флаги (JetBrains — ``--experimental``).
    """
    paths = {key: value for key, value in overrides.items() if "." in key}
    return paths, [key for key in overrides if "." not in key]


def foreign_flags_notice(program: str, names: Iterable[str]) -> List[str]:
    """Строки stderr о пропущенных флагах; у прежнего плоского имени —
    подсказка пути (``--max_steps`` → ``--agent.max_steps``)."""
    from gc_core.config_modules.path_hints import suggest_paths

    lines = []
    for name in names:
        hints = suggest_paths(name)
        hint = (
            f" (настройка — путь через точку: --{hints[0]})"
            if len(hints) == 1
            else ""
        )
        lines.append(f"[{program}] флаг --{name} пропущен{hint}")
    return lines


def apply_entry_overrides(loaded, overrides: Dict[str, Any]):
    """Правки CLI поверх загруженных настроек + уведомление легаси-форса.

    Ошибки (``UnknownSettingError``, ``ValidationError``, ``ValueError``) —
    наверх: текст для stderr даёт ``override_error_text``.
    """
    from gc_core.config import apply_cli_overrides, cli_forces_legacy_model

    if not overrides:
        return loaded
    loaded = apply_cli_overrides(loaded, extra_overrides=overrides)
    if cli_forces_legacy_model(loaded):
        loaded = loaded.with_runtime(
            notices=(*loaded.runtime.notices, LEGACY_FORCE_NOTICE)
        )
    return loaded


def override_error_text(exc: Exception) -> str:
    """Отказ правки для stderr — без значения (в нём может быть токен)."""
    from pydantic import ValidationError

    from gc_core.config_modules.path_hints import UnknownSettingError
    from gc_core.config_modules.root import schema_error_details

    if isinstance(exc, UnknownSettingError):
        return str(exc)
    if isinstance(exc, ValidationError):
        return f"некорректный параметр: {schema_error_details(exc)}"
    return f"некорректный параметр: {exc}"


__all__ = [
    "LEGACY_FORCE_NOTICE",
    "SETTING_FLAGS",
    "add_setting_flags",
    "apply_entry_overrides",
    "coerce_cli_value",
    "foreign_flags_notice",
    "override_error_text",
    "parse_override_args",
    "setting_flag_overrides",
    "split_setting_paths",
]
