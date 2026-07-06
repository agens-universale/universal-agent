"""Единый канал не-блокирующих стартовых уведомлений (CLI + TUI).

Модуль-владелец презентации/политики (как onboarding.py). Контроллеры (cli, tui)
зовут collect_startup_notices() и рендерят по-своему. Онбординг (готовность
конфига) — отдельно (onboarding.py); здесь — не-блокирующие замечания.
"""
from __future__ import annotations

from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple


class Notice(NamedTuple):
    level: str    # "warning" | "info"
    code: str     # стабильный id (для дедупа/тестов)
    message: str  # человекочитаемо, по-русски
    highlights: Tuple[str, ...] = ()  # тех. имена в message (тулы/сервера/ключи) для выделения в TUI


_DEPRECATED_KEYS = {"backend.stream": "tui.stream"}


def _stop_tools_unknown(config_dict: Dict[str, Any], builtin_tool_names: Set[str]) -> List[Notice]:
    raw = config_dict.get("stop_tools")
    if not isinstance(raw, list):
        return []
    stop = {s.strip() for s in raw if isinstance(s, str) and s.strip()}
    unknown = sorted(stop - set(builtin_tool_names))
    if not unknown:
        return []
    return [Notice(
        "warning", "stop_tools_unknown",
        "stop_tools: имена не совпали ни с одним встроенным тулом и "
        f"проигнорированы: {', '.join(unknown)}.",
        highlights=("stop_tools", *unknown),
    )]


def _mcp_failed(mcp_status: Optional[Dict[str, Any]]) -> List[Notice]:
    if not isinstance(mcp_status, dict):
        return []
    failed = sorted(
        name for name, st in mcp_status.items()
        if isinstance(st, dict) and st.get("status") == "failed"
    )
    if not failed:
        return []
    return [Notice(
        "warning", "mcp_failed",
        f"MCP не подключились: {', '.join(failed)}. Проверьте конфиг/доступность (/mcp).",
        highlights=tuple(failed),
    )]


def _deprecated(deprecated_keys_used: Optional[List[str]]) -> List[Notice]:
    out: List[Notice] = []
    for key in deprecated_keys_used or []:
        repl = _DEPRECATED_KEYS.get(key)
        if repl:
            out.append(Notice("info", "deprecated_key",
                              f"{key} устарел — используйте {repl}.",
                              highlights=(key, repl)))
    return out


def _agentsmd(config_dict: Dict[str, Any], *, is_tui: bool) -> List[Notice]:
    override = bool(config_dict.get("agentsmd_override"))
    if not override:
        return []
    if not is_tui:
        # Вне TUI слои L1–L3 не грузятся → флаг no-op (при любом agent_md_path).
        return [Notice("warning", "agentsmd_override_cli_noop",
                       "--agentsmd-override задан, но не действует вне TUI-режима.",
                       highlights=("--agentsmd-override",))]
    if config_dict.get("agent_md_path"):
        # В TUI при заданном пути: путь главнее флага → override не применён.
        # Эквивалент warning из build_instructions (условие: is_tui and agentsmd_override).
        return [Notice("warning", "agentsmd_override_ignored",
                       "agentsmd_override задан, но не применён: задан agent_md_path "
                       "(путь главнее флага).",
                       highlights=("agentsmd_override", "agent_md_path"))]
    return []


def collect_startup_notices(
    config_dict: Dict[str, Any],
    builtin_tool_names: Set[str],
    mcp_status: Optional[Dict[str, Any]],
    *,
    is_tui: bool,
    deprecated_keys_used: Optional[List[str]] = None,
) -> List[Notice]:
    """Единая точка сборки. Warnings перед infos, внутри — по источнику."""
    warns: List[Notice] = []
    infos: List[Notice] = []
    for n in (
        _stop_tools_unknown(config_dict, builtin_tool_names)
        + _mcp_failed(mcp_status)
        + _agentsmd(config_dict, is_tui=is_tui)
        + _deprecated(deprecated_keys_used)
    ):
        (warns if n.level == "warning" else infos).append(n)
    return warns + infos


def render_notices_for_cli(notices: List[Notice]) -> str:
    """Строки для stderr: по одной, с глифом уровня."""
    glyph = {"warning": "⚠", "info": "ℹ"}
    return "\n".join(f"{glyph.get(n.level, '·')} {n.message}" for n in notices)
