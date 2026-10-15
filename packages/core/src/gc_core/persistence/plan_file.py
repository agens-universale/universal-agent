"""Файл плана сессии для человека: открыть в редакторе, переслать, сохранить.

Односторонний экспорт. Программа его обратно не читает, в контекст модели он не
попадает; канон плана — состояние сессии. Один файл на сессию: новый или
доработанный план пишется поверх прежнего.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Mapping

from gc_core.plan_store import (
    normalize_plan_block,
    plan_accepted_mode,
    plan_status_label,
)
from gc_core.security.permission_mode import mode_label
from gc_core.utils.atomic_io import atomic_replace

_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip


def _absolute_local(iso_utc: str) -> str:
    """«13 октября 2026, 09:12» — абсолютная локальная дата: относительная
    («7 мин назад») в файле, открытом через сутки, была бы неправдой."""
    try:
        moment = datetime.fromisoformat(iso_utc).astimezone()
    except (TypeError, ValueError):
        return ""
    return (
        f"{moment.day} {_MONTHS[moment.month - 1]} {moment.year}, "
        f"{moment:%H:%M}"
    )


def render_plan_file(plan: Mapping[str, Any], thread_id: str) -> str:
    """Текст файла: заголовок, статус подписью, дата, затем план как есть."""
    block = normalize_plan_block(plan)
    if block is None:
        return ""
    status = plan_status_label(block)
    mode = plan_accepted_mode(block)
    if mode:
        status = f"{status} в {mode_label(mode)}"
    when = _absolute_local(block["updated_at"])
    meta = f"{status} · {when}" if when else status
    return f"# План сессии {thread_id}\n\n_{meta}_\n\n{block['text']}\n"


def write_plan_file(path: str, text: str) -> bool:
    """Записать файл, если его нет или содержимое отличается; True — писали.

    Сравнение вместо маркеров «что уже записано»: запись сама долечивает и
    сессии без файла, и прошлый сбой.
    """
    if not text:
        return False
    try:
        with open(path, encoding="utf-8") as current:
            if current.read() == text:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as out:
        out.write(text)
    atomic_replace(tmp_path, path)
    return True
