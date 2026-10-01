"""Пометка «изображение не просмотрено» (спека изображений §10).

Показывается, только когда модель только что оказалась слепой (новая пометка
`ImageRejects.mark`) и после этого смотреть некому (`Audience.nobody`). Зовут
двое: контроллер (отказ модели запроса) и тул `fs_view_image` (отказ «глаз»).
Канал — хук `RunContext.on_trace_note`: TUI рисует серую строку `[изображение]`
в ленте. Хука нет (CLI, сервер, SDK) — предупреждение в журнал прогона. Не
`runtime_notices`: пометка про конкретный запрос этой сессии, а реестр
уведомлений общий на процесс.
"""

from __future__ import annotations

import logging
from typing import Any

from gc_core.model_router import ResolvedModel

NOTE_MAIN_BLIND = (
    "{alias} не работает с изображениями — изображение не просмотрено. "
    "Модель для зрения можно выбрать в /settings → Модели."
)
NOTE_EYES_BLIND = (
    "Модель для зрения {alias} не работает с изображениями — изображение не "
    "просмотрено. Выберите другую в /settings → Модели."
)

_logger = logging.getLogger(__name__)


def note_alias(resolved: ResolvedModel) -> str:
    """Имя модели в пометке: псевдоним из реестра, в легаси — id модели."""
    return resolved.alias or resolved.model


def note_blind(
    ctx: Any, resolved: ResolvedModel, *, eyes: bool, logger: Any = None
) -> None:
    """Пометка про только что помеченную слепую модель: модель запроса
    (`eyes=False`, `NOTE_MAIN_BLIND`) или модель для зрения (`eyes=True`,
    `NOTE_EYES_BLIND`). Звать, только если пометка новая и смотреть некому."""
    template = NOTE_EYES_BLIND if eyes else NOTE_MAIN_BLIND
    emit_trace_note(
        ctx, template.format(alias=note_alias(resolved)), logger=logger
    )


def emit_trace_note(ctx: Any, text: str, *, logger: Any = None) -> None:
    """Отдать пометку хуку `ctx.on_trace_note`; хука нет — `warning` в
    переданный логгер, иначе в журнал прогона `ctx.logger`."""
    hook = getattr(ctx, "on_trace_note", None)
    if hook is not None:
        hook(text)
        return
    (logger or getattr(ctx, "logger", None) or _logger).warning(text)
