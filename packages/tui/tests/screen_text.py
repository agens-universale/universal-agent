"""Текст экранов /config и /mcp одной строкой — наблюдатель для тестов.

Прод-коду этот текст не нужен (экраны монтируют виджеты по частям), поэтому
сборка живёт здесь, а не методом экрана."""

from __future__ import annotations


def config_text(scr) -> str:
    """ConfigScreen: шапка профиля/уровня/модели + подсказка + пути + YAML."""
    from gc_tui.screens.config import _SNAPSHOT_HINT, _paths_block

    return (
        scr._profile_header().plain
        + "\n"
        + _SNAPSHOT_HINT
        + "\n"
        + _paths_block(
            scr._settings,
            scr._config_path,
            scr._project_local,
            scr._profile_path,
        ).plain
        + "\n"
        + scr._yaml_text()
    )


def mcp_text(scr) -> str:
    """McpScreen: карточки серверов + итоговая строка (или текст пустого экрана)."""
    empty = scr._empty_renderable()
    if empty is not None:
        return empty.plain
    parts = [card._renderable().plain for card in scr.cards]
    parts.append(scr._summary_renderable().plain)
    return "\n\n".join(parts)
