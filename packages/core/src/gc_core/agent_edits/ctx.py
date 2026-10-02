"""Чтение контекста запуска для журнала правок — общий дом двух каналов.

Эти пять функций нужны и рекордеру (правки инструментами), и шелл-каналу
(правки командами). Раньше они жили в `recorder` под подчёркиванием, и шелл-
канал тянул их оттуда как приватные — то есть у общего объекта владельцем
оказался файл, который первым его записал. Правило репозитория требует
названного владельца; он здесь.
"""

from __future__ import annotations

from typing import Any, Mapping


def config_from_ctx(ctx: Any) -> dict:
    config_dict = getattr(ctx, "config_dict", None) or {}
    return config_dict if isinstance(config_dict, dict) else {}


def agent_edits_config(ctx: Any) -> dict:
    """Секция ``agent_edits`` конфига на ctx — общая для обоих каналов записи."""
    section = config_from_ctx(ctx).get("agent_edits") or {}
    return section if isinstance(section, dict) else {}


# Служебный ключ конфига: режим запуска, объявленный фронтендом (в файле конфига
# его нет, как и `launch_root`). TUI узнаётся по признаку контекста `is_tui` —
# его ставит `acreate_run_context`, и дочерний контекст субагента его наследует.
RUN_MODE_KEY = "run_mode"
RUN_MODE_TUI = "tui"
RUN_MODE_CLI = "cli"
RUN_MODE_SERVER = "server"


def recording_enabled(ctx: Any) -> bool:
    """Включена ли запись правок в режиме этого запуска (``agent_edits.record``).

    Режим не объявлен (ядро как библиотека) или неизвестен — не пишем: журнал
    ведётся там, где его показывают или где его явно попросили.
    """
    if getattr(ctx, "is_tui", False):
        mode = RUN_MODE_TUI
    else:
        mode = config_from_ctx(ctx).get(RUN_MODE_KEY)
    if mode not in (RUN_MODE_TUI, RUN_MODE_CLI, RUN_MODE_SERVER):
        return False
    record = agent_edits_config(ctx).get("record")
    if not isinstance(record, dict):
        record = {}
    default = mode == RUN_MODE_TUI
    return bool(record.get(mode, default))


def identity_from_ctx(ctx: Any) -> tuple[str, str, str] | None:
    """threads_dir/system_id/thread_id — тем же способом, что sidecar."""
    threads_dir = getattr(ctx, "threads_dir", None)
    system_id = getattr(ctx, "system_id", None)
    thread_id = getattr(ctx, "thread_id", None)
    if not (
        isinstance(threads_dir, str)
        and threads_dir.strip()
        and isinstance(system_id, str)
        and system_id.strip()
        and isinstance(thread_id, str)
        and thread_id.strip()
    ):
        return None
    return threads_dir.strip(), system_id.strip(), thread_id.strip()


def launch_root_from_ctx(ctx: Any) -> str:
    launch_root = config_from_ctx(ctx).get("launch_root") or ""
    if isinstance(launch_root, str) and launch_root.strip():
        return launch_root
    workspace = getattr(ctx, "workspace", None)
    root = getattr(workspace, "root", None)
    return str(root) if root else ""


# Нижняя граница значения по ключу; всё, что ниже, — мусор, и читается дефолт
# схемы. Границы разные, потому что разный смысл нуля: `hunk_context_lines: 0`
# — законный дифф без контекстных строк, `max_diff_chars: 0` — обещанное обоими
# шаблонами конфига «без лимита» (`deltas.truncate_hunks`), а
# `snapshots_max_files: 0` — «не вытеснять»: `snapshots.evict_lru` отсекает его
# первой же строкой (`max_files <= 0 → return`) и снести ничего не может по
# построению. Единственный ключ, которому ноль запрещён, — `max_diff_source_bytes`:
# он помечает КАЖДУЮ правку `no_diff`, снимок «до» не снимается, и откат мёртв
# весь сеанс.
# Минимум — свойство ключа, а не точки вызова: те же четыре ключа читают оба
# канала записи (рекордер и шелл-делта), а поведение каналов обязано совпадать.
_MINIMUMS = {
    "hunk_context_lines": 0,
    "max_diff_chars": 0,
    "snapshots_max_files": 0,
}
_DEFAULT_MINIMUM = 1


def limit(explicit: int | None, ctx: Any, key: str, default: int) -> int:
    """Явно переданный лимит побеждает конфиг, конфиг — дефолт схемы.

    Значение из конфига санитизирует ``section_limit`` — см. там.
    """
    if explicit is not None:
        return explicit
    return section_limit(agent_edits_config(ctx), key, default)


def section_limit(section: Mapping[str, Any], key: str, default: int) -> int:
    """Числовой ключ секции ``agent_edits`` — ОДНО правило на всех читателей.

    Схема (`AgentEditsConfig`) объявляет эти ключи простым `int` без
    ограничений, то есть пропускает и отрицательное, а через слой оверрайдов
    доходит и нечисловое. Правило: ниже минимума ключа, нечисло и `bool`
    (`true` в yaml — не «единица», а мусор) — дефолт. Читатели — запись правок,
    экран изменений, лента TUI: одинаковый мусор в одной секции обязан вести
    себя одинаково. Особые значения (``replay_expanded_turns: 0`` — «все
    ходы») разбирает вызывающий ДО этой функции.
    """
    raw = (section or {}).get(key, default)
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= _MINIMUMS.get(key, _DEFAULT_MINIMUM) else default


def protected_extra_from_ctx(ctx: Any) -> list:
    protected = config_from_ctx(ctx).get("protected_paths") or {}
    return (
        list(protected.get("extra") or [])
        if isinstance(protected, dict)
        else []
    )
