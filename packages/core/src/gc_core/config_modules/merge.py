"""
Deep merge для layered config: project <cwd>/.gc/config.yml поверх глобального config.yml.

Правила:
- scalar (не dict, не list): overlay побеждает (override).
- dict + dict: рекурсивный deep merge — keys из overlay переписывают одноимённые
  в base, но не теряем те, что только в base.
- list + list: concat + dedup (сохраняя порядок: сначала base, потом новые из overlay).
- type mismatch (например, list vs string): overlay побеждает.

Не мутирует входные структуры — возвращает новые dicts/lists.

Concat+dedup для списков выбран из-за security-семантики ключевых list-полей
(stop_tools, url_blacklist, allowed_schemes и т.п.): project может ТОЛЬКО
расширить allow/deny списки, не сбросить global. То же самое делает Claude Code
в permissions.allow/permissions.deny.
"""

from copy import deepcopy
from typing import Any


def deep_merge(base: Any, overlay: Any) -> Any:
    """Слить overlay поверх base по правилам в module docstring."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        return _merge_dicts(base, overlay)
    if isinstance(base, list) and isinstance(overlay, list):
        return _merge_lists(base, overlay)
    return overlay


def _merge_dicts(base: dict, overlay: dict) -> dict:
    result = dict(base)
    for key, value in overlay.items():
        result[key] = (
            deep_merge(result[key], value) if key in result else value
        )
    return result


def _merge_lists(base: list, overlay: list) -> list:
    merged: list = list(base)
    for item in overlay:
        if item not in merged:
            merged.append(item)
    return merged


def merge_config_layers(base: dict, overlay: dict) -> dict:
    """Стопка конфигурации: kwargs алиаса — атомарные секции, прочее deep merge."""
    result = deep_merge(base, overlay)
    models = overlay.get("models")
    if not isinstance(models, dict):
        return result
    result["models"] = dict(result["models"])
    for alias, entry in models.items():
        if not isinstance(entry, dict):
            continue
        result["models"][alias] = dict(result["models"][alias])
        for section in ("main", "auxiliary"):
            if section in entry:
                result["models"][alias][section] = deepcopy(entry[section])
    return result
