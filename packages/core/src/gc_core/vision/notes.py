"""Пометка «изображение не просмотрено» (спека изображений §10).

Показывается, только когда модель только что оказалась слепой (новая пометка
`ImageRejects.mark`) и после этого смотреть некому (`Audience.nobody`). Зовут
двое: контроллер (отказ модели запроса) и тул `fs_view_image` (отказ «глаз»).
Канал — хук `RunContext.on_trace_note(TraceNote)`: TUI рисует `└ !` под
владельцем пометки — вызовом тула (`owner="call"`, его `call_id`) или ответом
(`owner="reply"`). Хука нет (CLI, сервер, SDK) — предупреждение в журнал
прогона. Не `runtime_notices`: пометка про конкретный запрос этой сессии, а
реестр уведомлений общий на процесс.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Literal

from gc_core.model_router import ResolvedModel
from gc_core.security.call_scope import current_call
from gc_core.vision.audience import viewing_eyes
from gc_core.vision.probe import ProbeStatus

NOTE_MAIN_BLIND = (
    "{alias} не работает с изображениями — изображение не просмотрено. "
    "Модель для зрения можно выбрать в /settings → Модели."
)
NOTE_EYES_BLIND = (
    "Модель для зрения {alias} не работает с изображениями — изображение не "
    "просмотрено. Выберите другую в /settings → Модели."
)
# Модель запроса не принимает картинки, но агент посмотрит их тулом зрения —
# не сбой, а факт про реплику (решение владельца 2026-10-24).
NOTE_MAIN_VIA_EYES = (
    "{alias} не принимает изображения · агент посмотрит через {eyes}"
)


@dataclass(frozen=True)
class TraceNote:
    """Пометка ленты с владельцем: `call` — вызов тула `call_id`, `reply` —
    ответ хода (пометка вне вызова тула). `tone`: `warn` — сбой, `info` —
    нейтральный факт."""

    text: str
    owner: Literal["reply", "call"]
    call_id: str | None = None
    tone: Literal["warn", "info"] = "warn"


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


def emit_trace_note(
    ctx: Any,
    text: str,
    *,
    logger: Any = None,
    tone: Literal["warn", "info"] = "warn",
) -> None:
    """Отдать пометку хуку `ctx.on_trace_note` как `TraceNote`; хука нет —
    тот же текст в переданный логгер, иначе в `ctx.logger` (`warn` —
    `warning`, `info` — `info`).
    Владелец — вызов тула, внутри которого пометка возникла (привязка
    `current_call()` с `call_id`), иначе ответ."""
    hook = getattr(ctx, "on_trace_note", None)
    if hook is not None:
        scope = current_call()
        call_id = scope.call_id if scope is not None else None
        owner = "call" if call_id else "reply"
        hook(TraceNote(text, owner, call_id, tone))
        return
    log = logger or getattr(ctx, "logger", None) or _logger
    (log.warning if tone == "warn" else log.info)(text)


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
    alias = note_alias(resolved)
    eyes = (
        viewing_eyes(ctx, state, resolved)
        if result.status is ProbeStatus.UNSUPPORTED
        else None
    )
    if eyes is not None:
        text = NOTE_MAIN_VIA_EYES.format(alias=alias, eyes=note_alias(eyes))
        emit_trace_note(ctx, text, tone="info")
        return
    reason = PROBE_REASON_RU.get(result.reason, "зрение не подтверждено")
    emit_trace_note(
        ctx,
        f"{alias}: {reason}. Пользовательское изображение не отправлено.",
    )
