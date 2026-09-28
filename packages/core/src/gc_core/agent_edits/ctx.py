"""Чтение контекста запуска для журнала правок — общий дом двух каналов.

Эти пять функций нужны и рекордеру (правки инструментами), и шелл-каналу
(правки командами). Раньше они жили в `recorder` под подчёркиванием, и шелл-
канал тянул их оттуда как приватные — то есть у общего объекта владельцем
оказался файл, который первым его записал. Правило репозитория требует
названного владельца; он здесь.
"""
from __future__ import annotations

from typing import Any


def config_from_ctx(ctx: Any) -> dict:
    config_dict = getattr(ctx, "config_dict", None) or {}
    return config_dict if isinstance(config_dict, dict) else {}


def agent_edits_config(ctx: Any) -> dict:
    """Секция ``agent_edits`` конфига на ctx — общая для обоих каналов записи."""
    section = config_from_ctx(ctx).get("agent_edits") or {}
    return section if isinstance(section, dict) else {}

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

    Значение из конфига санитизируется здесь: схема (`AgentEditsConfig`) объявляет
    эти ключи простым `int` без ограничений, то есть пропускает и
    отрицательное, а через слой оверрайдов доходит и нечисловое — прежде оно
    роняло `int()` внутри записи, и правка исчезала вместе с исключением.
    Ключи делит ровно один читатель — `edits_view.diff_limits`
    (`hunk_context_lines`, `max_diff_source_bytes`), и семантика у нас с ним
    одна: ниже минимума — дефолт, `bool` отсеивается. `main._agent_edits_limit`
    санитизирует ДРУГИЕ ключи (`trace_inline_max_lines`,
    `replay_expanded_turns`) и делает это иначе — клампом, без отсева `bool`;
    сравнивать себя с ним нельзя, общих ключей нет.

    Запись правок оставалась единственной, кто верил конфигу на слово, — и
    показывала «всё в порядке» там, где откат уже был недоступен.
    """
    if explicit is not None:
        return explicit
    raw = agent_edits_config(ctx).get(key, default)
    if isinstance(raw, bool):       # `true` в yaml — не «единица», а мусор
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= _MINIMUMS.get(key, _DEFAULT_MINIMUM) else default

def protected_extra_from_ctx(ctx: Any) -> list:
    protected = config_from_ctx(ctx).get("protected_paths") or {}
    return list(protected.get("extra") or []) if isinstance(protected, dict) else []
