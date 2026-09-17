"""Какие тулы РЕАЛЬНО есть в этом ране — источник правды для технических подсказок.

Канал уведомлений (`tool_usage_notice`, project_memory-хинты, todo-напоминание)
исторически спрашивал конфиг: «нет ли имени в ``stop_tools``». Этого мало — набор
рана сужается ещё тремя путями, о которых конфиг на этом уровне не знает:

* ``tools`` / ``stop_tools`` из файла кастомного сабагента — они едут в дочерний
  конфиг под ключами ``subagent_tools_*`` (см. ``tools.apply_subagent_tools_filter``),
  и проверки по ``stop_tools`` их не видят;
* harbor-режим, выбрасывающий host-only тулы;
* MCP-сервер, который не поднялся: правило могло быть написано на ``mcp_*``, в
  ``stop_tools`` его нет, а тула в наборе нет тоже.

Во всех трёх случаях подсказка про отобранный тул означает «велят вызвать то, чего
у тебя нет»: в лучшем случае шум, в худшем — модель слушается, ловит
``No tool named ...`` и упирается в ``max_unknown_tool_repeats``.

Контракт ``None``: набор НЕИЗВЕСТЕН (ctx без тулов, синтетические ctx в тестах) —
гейт не применяется, поведение остаётся прежним. Пустое множество — это знание о
том, что тулов нет, и оно гейт применяет. Разница намеренная: «не смогли узнать»
не должно молча выключать подсказки.
"""
from __future__ import annotations

from typing import Any, Optional, Set


def _names_from_source(source: Any) -> Optional[Set[str]]:
    """Имена тулов из ctx/state; ``None`` — источник ничего не знает о тулах."""
    if source is None:
        return None

    # RunContext.tools_data — словарь по именам, самый прямой источник.
    data = getattr(source, "tools_data", None)
    if isinstance(data, dict) and data:
        return {str(name) for name in data}

    # RunContext.tools_spec и State.tools — один и тот же список схем.
    spec = getattr(source, "tools_spec", None)
    if not isinstance(spec, list) or not spec:
        spec = getattr(source, "tools", None)
    if isinstance(spec, list) and spec:
        names = {
            str(item.get("name"))
            for item in spec
            if isinstance(item, dict) and item.get("name")
        }
        if names:
            return names
    return None


def available_tool_names(*sources: Any) -> Optional[Set[str]]:
    """Набор тулов рана из первого источника, который о них знает.

    Порядок источников задаёт вызывающий: обычно ``(ctx, state)`` — у ctx набор
    свежее, state годится как запасной путь.
    """
    for source in sources:
        names = _names_from_source(source)
        if names is not None:
            return names
    return None


def tool_is_available(names: Optional[Set[str]], tool_name: str) -> bool:
    """``True``, если тул есть в наборе ИЛИ набор неизвестен (см. контракт None)."""
    return names is None or tool_name in names
