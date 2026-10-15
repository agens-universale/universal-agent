"""Правила хранилищ тредов на базах, общие для Postgres и SQLite.

Проверки контракта (аренда, курсор списка) — в ``gc_core.threads.rules``;
здесь — то, как изменения записи ложатся на строку треда (``ThreadRow``).
"""

from __future__ import annotations

from gc_core.threads.models import SideChange, ThreadChange
from gc_core.threads.rules import patched_frontend

from gc_stores.rows import ThreadRow

#: Расширение имени вложения по типу.
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg"}


def apply_sets(row: ThreadRow, change: ThreadChange) -> None:
    """``set_*`` заменяют значение целиком (слияние — забота фасада)."""
    if change.set_state is not None:
        row.state = change.set_state
    if change.set_usage is not None:
        row.usage = change.set_usage
    if change.set_info is not None:
        row.info = change.set_info
    if change.set_runtime is not None:
        row.runtime = change.set_runtime
    if change.set_extra is not None:
        row.extra = change.set_extra


def apply_side(row: ThreadRow, change: SideChange) -> None:
    if change.set_runtime is not None:
        row.runtime = change.set_runtime
    if change.set_frontend is not None:
        name, state = change.set_frontend
        row.frontend[name] = state
    if change.patch_frontend is not None:
        name, patch = change.patch_frontend
        row.frontend[name] = patched_frontend(row.frontend.get(name), patch)
