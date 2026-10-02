"""Единый канал не-блокирующих стартовых уведомлений (CLI + TUI).

Модуль-владелец презентации/политики (как onboarding.py). Контроллеры (cli, tui)
зовут collect_startup_notices() и рендерят по-своему. Онбординг (готовность
конфига) — отдельно (onboarding.py); здесь — не-блокирующие замечания.
"""

from __future__ import annotations

from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from gc_core.config_modules.pricing import resolve_model_tariff
from gc_core.settings import AppConfig, settings_of


class Notice(NamedTuple):
    level: str  # "warning" | "info"
    code: str  # стабильный id (для дедупа/тестов)
    message: str  # человекочитаемо, по-русски
    highlights: Tuple[
        str, ...
    ] = ()  # тех. имена в message (тулы/сервера/ключи) для выделения в TUI


_DEPRECATED_KEYS = {"backend.stream": "tui.stream"}


def _stop_tools_unknown(
    settings: AppConfig,
    builtin_tool_names: Set[str],
    known_tool_names: Optional[Set[str]] = None,
) -> List[Notice]:
    stop = {s.strip() for s in settings.agent.stop_tools if s.strip()}
    # Каталог до вычитания (builtin + то, что собрал build_tools_data: скиллы,
    # MCP, сабагенты). Сверять с уже отфильтрованным набором нельзя: успешно
    # снятый тул снова станет опечаткой. Отдельного вечного списка skills_* нет —
    # их выключает тот же stop_tools, и известны они только если реально
    # попали в реестр.
    known = set(builtin_tool_names)
    if known_tool_names:
        known |= set(known_tool_names)
    unknown = sorted(stop - known)
    if not unknown:
        return []
    return [
        Notice(
            "warning",
            "stop_tools_unknown",
            f"stop_tools: имена не совпали ни с одним тулом и проигнорированы: {', '.join(unknown)}.",
            highlights=("stop_tools", *unknown),
        )
    ]


def _mcp_failed(mcp_status: Optional[Dict[str, Any]]) -> List[Notice]:
    if not isinstance(mcp_status, dict):
        return []
    failed = sorted(
        name
        for name, st in mcp_status.items()
        if isinstance(st, dict) and st.get("status") == "failed"
    )
    if not failed:
        return []
    return [
        Notice(
            "warning",
            "mcp_failed",
            f"MCP не подключились: {', '.join(failed)}. Проверьте конфиг/доступность (/mcp).",
            highlights=tuple(failed),
        )
    ]


def _deprecated(deprecated_keys_used: Optional[List[str]]) -> List[Notice]:
    out: List[Notice] = []
    for key in deprecated_keys_used or []:
        repl = _DEPRECATED_KEYS.get(key)
        if repl:
            out.append(
                Notice(
                    "info",
                    "deprecated_key",
                    f"{key} устарел — используйте {repl}.",
                    highlights=(key, repl),
                )
            )
    return out


def _pricing_no_rates(settings: AppConfig) -> List[Notice]:
    """Ставки не заданы: у провайдеров без фактической цены денег видно не будет."""
    pricing = settings.pricing
    if not pricing.enabled:
        return []
    if resolve_model_tariff(pricing, settings.agent.model) is not None:
        return []
    return [
        Notice(
            "info",
            "pricing_no_rates",
            "pricing: ставки не заданы. Провайдеры, которые сообщают фактическую стоимость "
            "(например, OpenRouter), всё равно будут показаны; для остальных стоимость "
            "останется скрытой — задайте цены в секции pricing (см. /config).",
            highlights=("pricing", "input", "output"),
        )
    ]


def _agentsmd(settings: AppConfig, *, is_tui: bool) -> List[Notice]:
    override = settings.tui.agentsmd_override
    if not override:
        return []
    if not is_tui:
        # Вне TUI слои L1–L3 не грузятся → флаг no-op (при любом agent_md_path).
        return [
            Notice(
                "warning",
                "agentsmd_override_cli_noop",
                "--agentsmd-override задан, но не действует вне TUI-режима.",
                highlights=("--agentsmd-override",),
            )
        ]
    if settings.paths.agent_md_path:
        # В TUI при заданном пути: путь главнее флага → override не применён.
        # Эквивалент warning из build_instructions (условие: is_tui and agentsmd_override).
        return [
            Notice(
                "warning",
                "agentsmd_override_ignored",
                "agentsmd_override задан, но не применён: задан agent_md_path (путь главнее флага).",
                highlights=("agentsmd_override", "agent_md_path"),
            )
        ]
    return []


def _compat(compat_notices: Optional[List[str]]) -> List[Notice]:
    return [
        Notice("warning", "config_compat", msg) for msg in compat_notices or []
    ]


def _upgrade(upgrade_info) -> List[Notice]:
    # upgrade_info: upgrade_gate.UpgradeInfo | None (duck-typed, без импорта политики)
    if not upgrade_info:
        return []
    old = getattr(upgrade_info, "old_version", None)
    new = getattr(upgrade_info, "new_version", "")
    frm = f" {old} →" if old else ""
    return [
        Notice(
            "info",
            "agent_upgraded",
            f"Агент обновлён:{frm} {new}.",
            highlights=(new,),
        )
    ]


def _subagent_definitions(
    settings: AppConfig, config_dict: Dict[str, Any]
) -> List[Notice]:
    """Пропущенные файлы кастомных сабагентов и отброшенные имена.

    Дискавери зовём здесь намеренно: он читает несколько локальных .md и потому
    дёшев (в отличие от MCP-дискавери, который ленив). Благодаря мемоизации в
    config_dict повторного чтения не будет, а уведомления доступны сразу на
    старте TUI, до первого запроса — иначе оператор узнал бы о пропущенном файле
    только после того, как сабагент не нашёлся.

    Молча пропущенный файл — ровно та же беда, что `stop_tools` с опечаткой:
    оператор что-то написал, оно не сработало, и в логе он это не увидит.
    """
    if not (settings.agent.subagents_dir or "").strip():
        return []
    try:
        from gc_core.tools.subagents.registry import (
            SUBAGENT_DIAGNOSTICS_KEY,
            resolve_subagent_definitions,
        )

        resolve_subagent_definitions(config_dict)
        from gc_core.config_modules.frozen import derived_cache

        records = (
            derived_cache(config_dict).get(SUBAGENT_DIAGNOSTICS_KEY) or []
        )
    except Exception:  # уведомления не должны ронять старт
        return []

    out: List[Notice] = []
    for rec in records:
        build = _SUBAGENT_NOTICE_BUILDERS.get(str(rec.get("code") or ""))
        if build is not None:
            out.append(build(rec))
    return out


def _notice_definition_invalid(rec: Dict[str, Any]) -> Notice:
    name = _basename(rec.get("path"))
    return Notice(
        "warning",
        "subagent_definition_invalid",
        f"Файл сабагента пропущен: {name} — {rec.get('reason')}",
        highlights=(name,),
    )


def _notice_name_collision(rec: Dict[str, Any]) -> Notice:
    name = _basename(rec.get("path"))
    taken = str(rec.get("subagent_name") or "")
    winner = rec.get("winner")
    by = "встроенным" if winner == "builtin" else f"файлом {_basename(winner)}"
    return Notice(
        "warning",
        "subagent_name_collision",
        f"Имя {taken} уже занято {by} — файл {name} пропущен.",
        highlights=(taken, name),
    )


def _notice_unknown_names(rec: Dict[str, Any]) -> Notice:
    names = [str(x) for x in (rec.get("names") or [])]
    return Notice(
        "warning",
        "use_local_subagents_unknown",
        f"use_local_subagents: имена не найдены ни среди встроенных, ни в папке и проигнорированы: {', '.join(names)}.",
        highlights=("use_local_subagents", *names),
    )


def _notice_max_count(rec: Dict[str, Any]) -> Notice:
    dropped = [str(x) for x in (rec.get("dropped") or [])]
    return Notice(
        "warning",
        "subagents_max_count_exceeded",
        f"Превышен subagents_max_count={rec.get('limit')} — не загружены: {', '.join(dropped)}.",
        highlights=("subagents_max_count", *dropped),
    )


def _notice_required_tools_kept(rec: Dict[str, Any]) -> Notice:
    name = _basename(rec.get("path"))
    names = [str(x) for x in (rec.get("names") or [])]
    return Notice(
        "warning",
        "subagent_required_tools_kept",
        f"{name}: служебные инструменты выдаются всегда, stop_tools их не "
        f"отбирает — запрет проигнорирован: {', '.join(names)}.",
        highlights=(name, *names),
    )


def _notice_dir_unavailable(rec: Dict[str, Any]) -> Notice:
    return Notice(
        "warning",
        str(rec.get("code") or ""),
        f"Папка кастомных сабагентов недоступна: {rec.get('dir')} — загружены только встроенные.",
        highlights=("subagents_dir",),
    )


_SUBAGENT_NOTICE_BUILDERS = {
    "subagent_definition_invalid": _notice_definition_invalid,
    "subagent_name_collision": _notice_name_collision,
    "use_local_subagents_unknown": _notice_unknown_names,
    "subagents_max_count_exceeded": _notice_max_count,
    "subagent_required_tools_kept": _notice_required_tools_kept,
    "subagents_dir_missing": _notice_dir_unavailable,
    "subagents_dir_unreadable": _notice_dir_unavailable,
}


def _basename(path: Any) -> str:
    text = str(path or "")
    return text.rsplit("/", 1)[-1] or text


def collect_startup_notices(
    config_dict: Dict[str, Any],
    builtin_tool_names: Set[str],
    mcp_status: Optional[Dict[str, Any]],
    *,
    is_tui: bool,
    extra: Sequence[Notice] = (),
    deprecated_keys_used: Optional[List[str]] = None,
    upgrade_info=None,
    compat_notices: Optional[List[str]] = None,
    known_tool_names: Optional[Set[str]] = None,
) -> List[Notice]:
    """Единая точка сборки. Warnings перед infos, внутри — по источнику.

    `extra` — уведомления фронтенда, которые ядру собирать нечем: голосовые
    живут в TUI, потому что и голос включается только там. Подставляются НА
    МЕСТО прежнего `_voice`, а не в хвост: внутри уровня порядок источников
    значим, и дописка в конец тихо переставила бы строки на экране.

    `known_tool_names` — каталог до вычитания ``stop_tools``. Без него
    валидатор видит только builtin: выключенный скилл-тул не считается
    известным, пока сборка не передала его имя.
    """
    settings = settings_of(config_dict)
    warns: List[Notice] = []
    infos: List[Notice] = []
    for n in (
        _stop_tools_unknown(settings, builtin_tool_names, known_tool_names)
        + _subagent_definitions(settings, config_dict)
        + _mcp_failed(mcp_status)
        + _agentsmd(settings, is_tui=is_tui)
        + list(extra)
        + _compat(compat_notices)
        + _pricing_no_rates(settings)
        + _deprecated(deprecated_keys_used)
        + _upgrade(upgrade_info)
    ):
        (warns if n.level == "warning" else infos).append(n)
    return warns + infos


def render_notices_for_cli(notices: List[Notice]) -> str:
    """Строки для stderr: по одной, с глифом уровня."""
    glyph = {"warning": "⚠", "info": "ℹ"}
    return "\n".join(f"{glyph.get(n.level, '·')} {n.message}" for n in notices)


def warning_toasts(
    notices: List[Notice], *, cap: int = 3
) -> Tuple[List[str], Optional[str], frozenset]:
    """Warning-уведомления для тоста при старте: до `cap` сообщений + сводный «+N ещё»
    (info НЕ тостим — только карточка/notices). Третий элемент — frozenset warning-кодов
    для сравнения «набор изменился» (тост показываем только on-change, см. вызывающего)."""
    warns = [n for n in notices if n.level == "warning"]
    codes = frozenset(n.code for n in warns)
    messages = [n.message for n in warns[:cap]]
    extra = len(warns) - cap
    summary = f"+{extra} ещё · /notices (F4)" if extra > 0 else None
    return messages, summary, codes
