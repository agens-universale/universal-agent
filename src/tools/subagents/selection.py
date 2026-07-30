"""Толкование ``use_local_subagents``: bool или список имён.

Список в ``use_local_subagents`` — это тот же «включено» плюс выбор конкретных имён:
оркестратор увидит только их, и это работает одинаково для встроенных и кастомных
сабагентов. ``use_server_subagents`` остаётся строго булевым: состав server-сабагентов
определяет бэкенд (ему уходит только ``metadata.useServerSubagents``), выбирать имена
на нашей стороне там нечем.

Модуль без зависимостей от остального проекта: его читают и сборка тулов, и
диспетчер, и ``agent_executor``, и ``manual_compaction``.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Set

_log = logging.getLogger(__name__)

_NAME_PREFIX = "subagent_"


def _normalize_name(raw: Any) -> str:
    """Привести элемент списка к каноническому имени тула.

    Префикс дописывается, если его не написали: имена всех сабагентов начинаются
    с ``subagent_`` по построению, поэтому ``worker`` и ``subagent_worker`` —
    заведомо одно и то же, и заставлять оператора помнить префикс незачем.
    """
    name = str(raw or "").strip()
    if not name:
        return ""
    return name if name.startswith(_NAME_PREFIX) else _NAME_PREFIX + name


def subagents_enabled(value: Any) -> bool:
    """Включены ли сабагенты вообще.

    ``True`` → да; непустой список → да (и с сужением состава); ``False``,
    ``None`` и **пустой список** → нет. Пустой список читается как «ни одного»
    осознанно: иначе он означал бы «все», и опечатка в YAML молча включала бы
    полный состав.
    """
    if isinstance(value, (list, tuple, set)):
        return any(_normalize_name(item) for item in value)
    return bool(value)


def subagents_allowlist(value: Any) -> Optional[Set[str]]:
    """Разрешённые имена сабагентов; ``None`` означает «все».

    ``True`` (историческая форма) → ``None``: сужения нет. Список → множество
    канонических имён.
    """
    if not isinstance(value, (list, tuple, set)):
        return None
    names = {_normalize_name(item) for item in value}
    names.discard("")
    return names or set()


def filter_subagents_by_allowlist(
    names: Iterable[str],
    value: Any,
    *,
    logger: Any = None,
    source: str = "use_local_subagents",
) -> Set[str]:
    """Пересечь доступные имена со списком из конфига.

    Имена из конфига, которых нет среди доступных, попадают в варнинг: опечатка
    в имени иначе просто уменьшила бы состав, и оператор не понял бы, почему
    сабагента нет. Старт при этом не прерывается — вся фича кастомных сабагентов
    построена на варнингах.
    """
    available = set(names)
    allowed = subagents_allowlist(value)
    if allowed is None:
        return available
    unknown = sorted(allowed - available)
    if unknown:
        (logger or _log).warning(
            "%s_unknown names=%s available=%s",
            source, ", ".join(unknown), ", ".join(sorted(available)),
            extra={
                "event": f"{source}_unknown",
                "names": unknown,
                "available": sorted(available),
            },
        )
    return available & allowed


__all__ = [
    "filter_subagents_by_allowlist",
    "subagents_allowlist",
    "subagents_enabled",
]
