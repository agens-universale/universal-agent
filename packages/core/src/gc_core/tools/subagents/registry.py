import logging
from collections.abc import Mapping
from typing import Any, Dict, List, Type

from gc_core.settings import AppConfig, LoadedSettings, settings_of
from gc_core.tools.subagents.abstract_subagent import AbstractSubagent
from gc_core.tools.subagents.code_planner import CodePlanner
from gc_core.tools.subagents.code_reviewer import CodeReviewer
from gc_core.tools.subagents.coder import CodeGenerator
from gc_core.tools.subagents.critic import Critic
from gc_core.tools.subagents.custom.file_definitions import (
    SubagentDefinition,
    discover_subagent_definitions,
    resolve_max_count,
)
from gc_core.tools.subagents.custom.file_subagent import FileDefinedSubagent
from gc_core.tools.subagents.planner import Planner
from gc_core.tools.subagents.selection import subagents_allowlist_ordered
from gc_core.tools.subagents.special_agent import SpecialAgent
from gc_core.tools.subagents.worker import Worker

SUBAGENT_CLASSES: List[Type[AbstractSubagent]] = [
    Worker,
    CodeGenerator,
    CodePlanner,
    Planner,
    Critic,
    CodeReviewer,
    SpecialAgent,
]

_SUBAGENT_MAP: Dict[str, Type[AbstractSubagent]] = {
    cls.agent_name: cls
    for cls in SUBAGENT_CLASSES  # type: ignore[attr-defined]
}

# Разобранные определения кэшируются на контексте рана (``RunContext.
# subagent_definitions``/``subagent_diagnostics``): реестр дёргается на КАЖДЫЙ
# вызов тула (диспетчер apply_tool), и читать папку каждый раз было бы
# расточительно. Кэш при контексте, а не глобальный: в серверном режиме раны идут
# в разных потоках со своими настройками, и общий кэш отдавал бы чужие
# определения. Durable-сборка (Temporal) кладёт туда готовые определения заранее —
# workflow на диск не ходит.
_DEFS_ATTR = "subagent_definitions"
# Структурные факты о пропущенных файлах и отброшенных именах — для стартовых
# уведомлений TUI. Лог читают не все, а «файл молча пропущен» оператор обязан
# увидеть на экране.
_DIAGNOSTICS_ATTR = "subagent_diagnostics"

_log = logging.getLogger(__name__)


def builtin_subagent_names() -> set:
    """Имена встроенных сабагентов — они выигрывают коллизию у кастомных."""
    return set(_SUBAGENT_MAP.keys())


def _warn_unknown_allowlist(
    log: Any,
    ordered: List[str],
    by_name: Dict[str, Any],
    builtin_names: set,
    diagnostics,
) -> None:
    unknown = [
        n for n in ordered if n not in by_name and n not in builtin_names
    ]
    if not unknown:
        return
    available = sorted(set(by_name) | set(builtin_names))
    log.warning(
        "use_local_subagents_unknown names=%s available=%s",
        ", ".join(unknown),
        ", ".join(available),
        extra={
            "event": "use_local_subagents_unknown",
            "names": unknown,
            "available": available,
        },
    )
    if diagnostics is not None:
        diagnostics.append(
            {"code": "use_local_subagents_unknown", "names": unknown}
        )


def _limit_definitions(
    definitions: List[SubagentDefinition],
    max_count: int,
    log: Any,
    diagnostics,
) -> List[SubagentDefinition]:
    if len(definitions) <= max_count:
        return definitions
    dropped = [d.name for d in definitions[max_count:]]
    log.warning(
        "subagents_max_count_exceeded limit=%s dropped=%s",
        max_count,
        ", ".join(dropped),
        extra={
            "event": "subagents_max_count_exceeded",
            "limit": max_count,
            "dropped": dropped,
        },
    )
    if diagnostics is not None:
        diagnostics.append(
            {
                "code": "subagents_max_count_exceeded",
                "limit": max_count,
                "dropped": dropped,
            }
        )
    return definitions[:max_count]


def _apply_allowlist_and_limit(
    definitions: List[SubagentDefinition],
    settings: AppConfig,
    *,
    builtin_names: set,
    logger: Any = None,
    diagnostics: List[Dict[str, Any]] | None = None,
) -> List[SubagentDefinition]:
    """Отобрать кастомные по ``use_local_subagents`` и урезать по лимиту.

    Список в ``use_local_subagents`` сужает ТОЛЬКО кастомных: встроенные семь
    доступны всегда, пока фича включена вообще (``false`` выключает всех). Имя
    встроенного в списке поэтому безвредно — он и так включён.

    ``subagents_max_count`` тоже относится только к кастомным. Когда список задан,
    урезаем по позиции В СПИСКЕ, а не по имени файла: оператор перечислил их в
    порядке приоритета.
    """
    log = logger or _log
    ordered = subagents_allowlist_ordered(settings.agent.use_local_subagents)

    if ordered is not None:
        by_name = {d.name: d for d in definitions}
        _warn_unknown_allowlist(
            log, ordered, by_name, builtin_names, diagnostics
        )
        definitions = [by_name[n] for n in ordered if n in by_name]

    return _limit_definitions(
        definitions, resolve_max_count(settings), log, diagnostics
    )


def _is_run_context(source: Any) -> bool:
    """Источник — контекст рана (у него свой кэш), а не сами настройки."""
    return source is not None and not isinstance(
        source, (AppConfig, LoadedSettings, Mapping)
    )


def _remember(source: Any, definitions: Any, diagnostics: Any) -> None:
    """Запомнить итог дискавери на контексте рана (если источник — контекст)."""
    if not _is_run_context(source):
        return
    try:
        setattr(source, _DEFS_ATTR, definitions)
        setattr(source, _DIAGNOSTICS_ATTR, diagnostics)
    except (AttributeError, TypeError):
        # Неизменяемый двойник контекста: кэша нет, дискавери повторится.
        pass


def subagent_diagnostics(settings: Any) -> List[Dict[str, Any]]:
    """Диагностика дискавери кастомных сабагентов (для стартовых уведомлений).

    С контекстом рана — его сохранённая диагностика (дискавери при первом
    обращении); с настройками — свежий дискавери.
    """
    if settings is None:
        return []
    cached = getattr(settings, _DIAGNOSTICS_ATTR, None)
    if _is_run_context(settings) and cached is not None:
        return list(cached)
    _, diagnostics = _discover(settings)
    return list(diagnostics)


def resolve_subagent_definitions(
    settings: Any = None,
    *,
    logger: Any = None,
) -> Dict[str, SubagentDefinition]:
    """Определения кастомных сабагентов (с мемоизацией на контексте рана).

    ``settings`` — контекст рана (предпочтительно: определения запоминаются в
    ``ctx.subagent_definitions``, а заранее положенные туда возвращаются без
    дискавери) или настройки — всё, что принимает ``settings_of``. ``None`` →
    кастомных нет вовсе: так вызывают тесты и любой код, которому нужен только
    встроенный состав.
    """
    if settings is None:
        return {}
    seeded = getattr(settings, _DEFS_ATTR, None)
    if _is_run_context(settings) and seeded is not None:
        return seeded
    resolved, diagnostics = _discover(settings, logger=logger)
    _remember(settings, resolved, diagnostics)
    return resolved


def _discover(
    settings: Any, *, logger: Any = None
) -> tuple[Dict[str, SubagentDefinition], List[Dict[str, Any]]]:
    """Прочитать папку сабагентов: ``(определения, диагностика)``."""
    app = settings_of(settings)
    builtin = builtin_subagent_names()
    diagnostics: List[Dict[str, Any]] = []
    definitions = discover_subagent_definitions(
        app,
        builtin_names=builtin,
        logger=logger,
        diagnostics=diagnostics,
    )
    definitions = _apply_allowlist_and_limit(
        definitions,
        app,
        builtin_names=builtin,
        logger=logger,
        diagnostics=diagnostics,
    )
    for definition in definitions:
        banned = FileDefinedSubagent.required_tools_banned(definition)
        if banned:
            # Запрет не исполняется — служебное ядро выдаётся всегда. Но указание
            # оператора явное, поэтому о его игнорировании сообщаем.
            diagnostics.append(
                {
                    "code": "subagent_required_tools_kept",
                    "path": definition.source_path,
                    "names": banned,
                }
            )
            (logger or _log).warning(
                "subagent_required_tools_kept path=%s names=%s "
                "(служебные инструменты выдаются всегда, stop_tools их не отбирает)",
                definition.source_path,
                ", ".join(banned),
                extra={
                    "event": "subagent_required_tools_kept",
                    "path": definition.source_path,
                    "names": banned,
                },
            )

    resolved = {definition.name: definition for definition in definitions}
    return resolved, diagnostics


def get_subagent_class(tool_name: str, settings: Any = None) -> Any:
    builtin = _SUBAGENT_MAP.get(tool_name)
    if builtin is not None:
        return builtin
    definition = resolve_subagent_definitions(settings).get(tool_name)
    return (
        FileDefinedSubagent.bound(definition)
        if definition is not None
        else None
    )


def is_subagent_tool(tool_name: str, settings: Any = None) -> bool:
    if tool_name in _SUBAGENT_MAP:
        return True
    return tool_name in resolve_subagent_definitions(settings)


def get_subagents_tools_data(
    settings: Any = None,
    *,
    logger: Any = None,
) -> Dict[str, Dict[str, Any]]:
    """Собрать tools_data записи для встроенных и кастомных сабагентов."""
    result: Dict[str, Dict[str, Any]] = {}
    for cls in SUBAGENT_CLASSES:
        spec = cls.get_agent_specification()
        result[spec["name"]] = {
            "spec": spec,
            "description": spec.get("description", ""),
            "tool_fabric": None,
        }

    for name, definition in resolve_subagent_definitions(
        settings, logger=logger
    ).items():
        bound = FileDefinedSubagent.bound(definition)
        spec = bound.get_agent_specification()
        result[name] = {
            "spec": spec,
            "description": definition.description,
            "tool_fabric": None,
        }
    return result
