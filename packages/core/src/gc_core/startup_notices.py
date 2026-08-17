"""Единый канал не-блокирующих стартовых уведомлений (CLI + TUI).

Модуль-владелец презентации/политики (как onboarding.py). Контроллеры (cli, tui)
зовут collect_startup_notices() и рендерят по-своему. Онбординг (готовность
конфига) — отдельно (onboarding.py); здесь — не-блокирующие замечания.
"""
from __future__ import annotations

from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

from gc_core.config_modules.pricing import resolve_model_tariff


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


def _pricing_zero_rates(config_dict: Dict[str, Any]) -> List[Notice]:
    pricing = config_dict.get("pricing")
    if not isinstance(pricing, dict) or not pricing.get("enabled"):
        return []
    tariff = resolve_model_tariff(pricing, config_dict.get("model"))
    if tariff is None:
        return []
    if tariff.input != 0 or tariff.output != 0:
        return []
    return [Notice(
        "info", "pricing_zero_rates",
        "pricing: тариф включён, но ставки input и output равны 0 — "
        "стоимость в футере и отчётах будет 0.0. Задайте реальные цены в секции pricing в конфиге агента (см. /config).",
        highlights=("pricing", "input", "output"),
    )]


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


def _compat(compat_notices: Optional[List[str]]) -> List[Notice]:
    return [Notice("warning", "config_compat", msg) for msg in compat_notices or []]


def _upgrade(upgrade_info) -> List[Notice]:
    # upgrade_info: upgrade_gate.UpgradeInfo | None (duck-typed, без импорта политики)
    if not upgrade_info:
        return []
    old = getattr(upgrade_info, "old_version", None)
    new = getattr(upgrade_info, "new_version", "")
    frm = f" {old} →" if old else ""
    return [Notice("info", "agent_upgraded",
                   f"Агент обновлён:{frm} {new}.",
                   highlights=(new,))]


def _subagent_definitions(config_dict: Dict[str, Any]) -> List[Notice]:
    """Пропущенные файлы кастомных сабагентов и отброшенные имена.

    Дискавери зовём здесь намеренно: он читает несколько локальных .md и потому
    дёшев (в отличие от MCP-дискавери, который ленив). Благодаря мемоизации в
    config_dict повторного чтения не будет, а уведомления доступны сразу на
    старте TUI, до первого запроса — иначе оператор узнал бы о пропущенном файле
    только после того, как сабагент не нашёлся.

    Молча пропущенный файл — ровно та же беда, что `stop_tools` с опечаткой:
    оператор что-то написал, оно не сработало, и в логе он это не увидит.
    """
    if not str(config_dict.get("subagents_dir") or "").strip():
        return []
    try:
        from gc_core.tools.subagents.registry import SUBAGENT_DIAGNOSTICS_KEY, resolve_subagent_definitions

        resolve_subagent_definitions(config_dict)
        records = config_dict.get(SUBAGENT_DIAGNOSTICS_KEY) or []
    except Exception:  # noqa: BLE001 — уведомления не должны ронять старт
        return []

    out: List[Notice] = []
    for rec in records:
        code = str(rec.get("code") or "")
        if code == "subagent_definition_invalid":
            name = _basename(rec.get("path"))
            out.append(Notice(
                "warning", code,
                f"Файл сабагента пропущен: {name} — {rec.get('reason')}",
                highlights=(name,),
            ))
        elif code == "subagent_name_collision":
            name = _basename(rec.get("path"))
            taken = str(rec.get("subagent_name") or "")
            winner = rec.get("winner")
            by = "встроенным" if winner == "builtin" else f"файлом {_basename(winner)}"
            out.append(Notice(
                "warning", code,
                f"Имя {taken} уже занято {by} — файл {name} пропущен.",
                highlights=(taken, name),
            ))
        elif code == "use_local_subagents_unknown":
            names = [str(x) for x in (rec.get("names") or [])]
            out.append(Notice(
                "warning", code,
                "use_local_subagents: имена не найдены ни среди встроенных, ни в папке "
                f"и проигнорированы: {', '.join(names)}.",
                highlights=("use_local_subagents", *names),
            ))
        elif code == "subagents_max_count_exceeded":
            dropped = [str(x) for x in (rec.get("dropped") or [])]
            out.append(Notice(
                "warning", code,
                f"Превышен subagents_max_count={rec.get('limit')} — не загружены: "
                f"{', '.join(dropped)}.",
                highlights=("subagents_max_count", *dropped),
            ))
        elif code == "subagent_required_tools_kept":
            name = _basename(rec.get("path"))
            names = [str(x) for x in (rec.get("names") or [])]
            out.append(Notice(
                "warning", code,
                f"{name}: служебные инструменты выдаются всегда, stop_tools их не "
                f"отбирает — запрет проигнорирован: {', '.join(names)}.",
                highlights=(name, *names),
            ))
        elif code in ("subagents_dir_missing", "subagents_dir_unreadable"):
            out.append(Notice(
                "warning", code,
                f"Папка кастомных сабагентов недоступна: {rec.get('dir')} — "
                "загружены только встроенные.",
                highlights=("subagents_dir",),
            ))
    return out


def _basename(path: Any) -> str:
    text = str(path or "")
    return text.rsplit("/", 1)[-1] or text


def collect_startup_notices(
    config_dict: Dict[str, Any],
    builtin_tool_names: Set[str],
    mcp_status: Optional[Dict[str, Any]],
    *,
    is_tui: bool,
    deprecated_keys_used: Optional[List[str]] = None,
    upgrade_info=None,
    compat_notices: Optional[List[str]] = None,
) -> List[Notice]:
    """Единая точка сборки. Warnings перед infos, внутри — по источнику."""
    warns: List[Notice] = []
    infos: List[Notice] = []
    for n in (
        _stop_tools_unknown(config_dict, builtin_tool_names)
        + _subagent_definitions(config_dict)
        + _mcp_failed(mcp_status)
        + _agentsmd(config_dict, is_tui=is_tui)
        + _compat(compat_notices)
        + _pricing_zero_rates(config_dict)
        + _deprecated(deprecated_keys_used)
        + _upgrade(upgrade_info)
    ):
        (warns if n.level == "warning" else infos).append(n)
    return warns + infos


def render_notices_for_cli(notices: List[Notice]) -> str:
    """Строки для stderr: по одной, с глифом уровня."""
    glyph = {"warning": "⚠", "info": "ℹ"}
    return "\n".join(f"{glyph.get(n.level, '·')} {n.message}" for n in notices)


def warning_toasts(notices: List[Notice], *, cap: int = 3) -> "Tuple[List[str], Optional[str], frozenset]":
    """Warning-уведомления для тоста при старте: до `cap` сообщений + сводный «+N ещё»
    (info НЕ тостим — только карточка/notices). Третий элемент — frozenset warning-кодов
    для сравнения «набор изменился» (тост показываем только on-change, см. вызывающего)."""
    warns = [n for n in notices if n.level == "warning"]
    codes = frozenset(n.code for n in warns)
    messages = [n.message for n in warns[:cap]]
    extra = len(warns) - cap
    summary = f"+{extra} ещё · /notices (F4)" if extra > 0 else None
    return messages, summary, codes
