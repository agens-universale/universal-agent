"""Запись треда глазами экранов TUI.

Экраны читают типизированную запись (``ThreadRecord``): ``state``,
``usage``, ``runtime``, ``info`` — как есть. Здесь только два поля, которые
лежат не там, где их ищет экран:

* секция TUI — ``frontend["tui"]``, а заголовок сессии файловое хранилище
  держит в карточке треда (``info.title``);
* исход сессии пишет TUI (``tui.status``), а CLI и сервер — ключом верхнего
  уровня (``extra["status"]``).
"""

from __future__ import annotations

from typing import Any, Optional

from gc_core.threads.models import ThreadRecord

_TUI = "tui"


def tui_section(record: Optional[ThreadRecord]) -> dict[str, Any]:
    """Секция TUI треда вместе с заголовком; треда нет — ``{}``."""
    if record is None:
        return {}
    section = dict(record.frontend.get(_TUI) or {})
    if record.info.title is not None:
        section["title"] = record.info.title
    return section


def outcome_status(record: Optional[ThreadRecord]) -> Optional[str]:
    """Исход сессии: записанный TUI, иначе — CLI или сервером; ``None`` —
    не записан."""
    if record is None:
        return None
    status = tui_section(record).get("status") or record.extra.get("status")
    return status if isinstance(status, str) else None
