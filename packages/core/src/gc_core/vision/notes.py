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
import threading
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
_probe_notice_lock = threading.Lock()


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


PROBE_REASON_RU = {
    "code_mismatch": "модель не прочитала проверочную картинку",
    "output_limit": "ответ на проверку усечён по лимиту 512 токенов",
    "image_unsupported": "маршрут отказал в поддержке изображений",
    "network": "временный сетевой сбой проверки; повтор возможен в новом ходе после паузы",
    "probe_payload_rejected": "маршрут отклонил даже маленькую проверочную картинку (413)",
    "access_denied": "ошибка доступа при проверке зрения",
    "route_configuration": "ошибка настроек маршрута при проверке зрения",
    "probe_cancelled": "проверка зрения отменена",
}


def note_probe(ctx, state, key, resolved, result):
    notices = getattr(state, "vision_probe_notices", None)
    if notices is None:
        notices = state.vision_probe_notices = set()
    identity = (getattr(state, "vision_probe_turn", None), key, result.reason)
    with _probe_notice_lock:
        if identity in notices:
            return
        notices.add(identity)
    reason = PROBE_REASON_RU.get(result.reason, "зрение не подтверждено")
    emit_trace_note(
        ctx,
        f"{note_alias(resolved)}: {reason}. Пользовательское изображение не отправлено.",
    )
