"""Автозапуск HTTP/SSE MCP-daemon'ов перед запуском агента.

Жил в ядре (`recommended_mcp.runtime.daemon_autostart`) как мост «фронтенд → рецепты», хотя
потребителей в самом ядре у него не было ни одного: зовут его `gc_cli.cli` и
`gc_tui`. Ради этого ядро импортировало `recommended_mcp` — пакет, которого нет в
его зависимостях и не может быть: `gc-mcp` сам зависит от `gc-core`, вышло бы
кольцо дистрибутивов. Переехал сюда 27.08.2026: код принадлежит рецептам (это
автозапуск ИХ daemon'ов), а все три профиля пинят `gc-mcp` жёстко.

Прежней охраны `except ImportError` здесь НЕТ и не нужно: рецепты — соседний
модуль того же дистрибутива. Свойство, ради которого охрана была точной, при этом
только усилилось: ошибка импорта ВНУТРИ рецептов (опечатка, отсутствующая
зависимость) летит как есть, а не выдаётся за «рецептов не установлено». Раньше
это обеспечивала сверка `exc.name`; теперь — отсутствие перехвата вовсе.
Сторож — `packages/mcp/tests/test_recipes_guard.py`.
"""

from __future__ import annotations

from typing import Any

_supervisor: Any = None


def ensure_mcp_daemons(config_dict: dict[str, Any]) -> None:
    """Поднять недостающие MCP-daemon'ы перед запуском агента."""
    global _supervisor
    from recommended_mcp.autostart import ensure_daemons_for_agent

    _supervisor = ensure_daemons_for_agent(config_dict)


def shutdown_mcp_daemons() -> None:
    """Остановить daemon'ы, поднятые ensure_mcp_daemons() в этом процессе."""
    global _supervisor
    if _supervisor is None:
        return
    try:
        _supervisor.shutdown_managed()
    finally:
        _supervisor = None
