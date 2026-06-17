"""Auto-wire MCP в machine-managed ~/.gc/config.local.yml.

Контроллер: читает agent_config.snippet.yml рецепта, резолвит плейсхолдеры
{npx}/{cache}, upsert'ит mcp.servers.<name> в managed-файл. config.yml
пользователя не трогается."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from mcp_runtime.paths import managed_config_path

_PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")


def resolve_placeholders(value: Any, *, npx: str, cache: str) -> Any:
    """Рекурсивно подставить {npx}/{cache} во все строки. Неизвестный {x} —
    оставить (вызывающий печатает warn)."""
    if isinstance(value, str):
        return value.replace("{npx}", npx).replace("{cache}", cache)
    if isinstance(value, dict):
        return {k: resolve_placeholders(v, npx=npx, cache=cache) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_placeholders(v, npx=npx, cache=cache) for v in value]
    return value


def read_snippet(recipe_dir: Path) -> dict[str, Any]:
    """Прочитать agent_config.snippet.yml (mapping {server_key: config})."""
    snippet = recipe_dir / "agent_config.snippet.yml"
    if not snippet.is_file():
        raise FileNotFoundError(f"snippet не найден: {snippet}")
    data = yaml.safe_load(snippet.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or not data:
        raise ValueError(f"snippet пуст/некорректен: {snippet}")
    return data


def _load_managed() -> dict[str, Any]:
    path = managed_config_path()
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _save_managed(data: dict[str, Any]) -> None:
    path = managed_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def wired_servers() -> dict[str, Any]:
    """mcp.servers из managed-файла (= что подключено через gc-mcp)."""
    return ((_load_managed().get("mcp") or {}).get("servers") or {})


def wire_snippet(name: str, recipe_dir: Path, *, npx: str, cache: str) -> list[str]:
    """Upsert mcp.servers.<name> из сниппета в managed-файл. Возвращает список
    оставшихся неизвестных плейсхолдеров (для warn)."""
    snippet = read_snippet(recipe_dir)
    # сниппет — {server_key: config}; берём единственный config-блок
    config_block = next(iter(snippet.values()))
    resolved = resolve_placeholders(config_block, npx=npx, cache=cache)

    data = _load_managed()
    data.setdefault("mcp", {}).setdefault("servers", {})[name] = resolved
    _save_managed(data)

    leftover = sorted({m for v in _iter_strings(resolved) for m in _PLACEHOLDER_RE.findall(v)})
    return leftover


def unwire(name: str) -> bool:
    """Убрать mcp.servers.<name> из managed-файла. True если был, False если нет."""
    data = _load_managed()
    servers = (data.get("mcp") or {}).get("servers") or {}
    if name not in servers:
        return False
    del servers[name]
    _save_managed(data)
    return True


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_strings(v)
