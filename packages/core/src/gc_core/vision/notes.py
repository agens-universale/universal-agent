"""Пометка «модель не принимает изображения» и сбоев проверки зрения (спека
изображений §10).

Пути два: проба маршрута (`note_probe`) и реальный отказ модели запроса
(`note_blind`, только когда смотреть некому — `Audience.nobody`); текст про
неприём изображений у них один (`unsupported_note`).
Канал — хук `RunContext.on_trace_note(TraceNote)`: TUI рисует `└` под
владельцем пометки — вызовом тула (`owner="call"`, его `call_id`) или ответом
(`owner="reply"`); сбой (`warn`) — с `!`, нейтральный факт (`info`) — без.
Хука нет (CLI, сервер, SDK) — сбой предупреждением в журнал прогона,
нейтральный факт — сведением. Не `runtime_notices`: пометка про конкретный запрос этой сессии, а
реестр уведомлений общий на процесс.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Literal

from gc_core.model_router import ResolvedModel
from gc_core.security.call_scope import current_call
from gc_core.vision.audience import route_id, viewing_eyes
from gc_core.vision.probe import ProbeStatus

# «Модель не принимает изображения» — один факт одним текстом на обоих путях
# (проба `note_probe` и реальный отказ `note_blind`); различается только
# окончание (решение владельца 2026-10-24). Собирает `unsupported_note`.
NOTE_UNSUPPORTED = "{alias} не принимает изображения"
NOTE_VIA_EYES = "агент посмотрит через {eyes}"
NOTE_NOT_VIEWED = "изображение не просмотрено"
NOTE_HINT_MAIN = (
    "выберите модель с поддержкой изображений или назначьте модель для "
    "зрения в настройках"
)
NOTE_HINT_EYES = "назначьте другую модель для зрения в настройках"
_NOTE_SEP = " · "

Tone = Literal["warn", "info"]


@dataclass(frozen=True)
class TraceNote:
    """Пометка ленты с владельцем: `call` — вызов тула `call_id`, `reply` —
    ответ хода (пометка вне вызова тула). `tone`: `warn` — сбой, `info` —
    нейтральный факт."""

    text: str
    owner: Literal["reply", "call"]
    call_id: str | None = None
    tone: Tone = "warn"


_logger = logging.getLogger(__name__)
_probe_notice_lock = threading.Lock()


def note_alias(resolved: ResolvedModel) -> str:
    """Имя модели в пометке: псевдоним из реестра, в легаси — id модели."""
    return resolved.alias or resolved.model


def unsupported_note(
    alias: str, *, viewer: str | None = None, eyes_role: bool = False
) -> tuple[str, Tone]:
    """Текст и тон пометки «``alias`` не принимает изображения»: смотрит
    модель зрения ``viewer`` — нейтральный `info`; иначе `warn` с выходом для
    модели запроса или (``eyes_role``) для самой модели зрения."""
    head = NOTE_UNSUPPORTED.format(alias=alias)
    if viewer is not None:
        return _NOTE_SEP.join(
            (head, NOTE_VIA_EYES.format(eyes=viewer))
        ), "info"
    hint = NOTE_HINT_EYES if eyes_role else NOTE_HINT_MAIN
    return _NOTE_SEP.join((head, NOTE_NOT_VIEWED, hint)), "warn"


def note_blind(
    ctx: Any, resolved: ResolvedModel, *, eyes: bool, logger: Any = None
) -> None:
    """Пометка про только что помеченную слепую модель: модель запроса
    (`eyes=False`) или модель для зрения (`eyes=True`). Звать, только если
    пометка новая и смотреть некому."""
    text, tone = unsupported_note(note_alias(resolved), eyes_role=eyes)
    emit_trace_note(ctx, text, logger=logger, tone=tone)


def emit_trace_note(
    ctx: Any,
    text: str,
    *,
    logger: Any = None,
    tone: Tone = "warn",
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
    if result.status is ProbeStatus.UNSUPPORTED:
        text, tone = _unsupported_probe_note(ctx, state, resolved)
        emit_trace_note(ctx, text, tone=tone)
        return
    reason = PROBE_REASON_RU.get(result.reason, "зрение не подтверждено")
    emit_trace_note(
        ctx,
        f"{alias}: {reason}. Пользовательское изображение не отправлено.",
    )


def _unsupported_probe_note(ctx, state, resolved) -> tuple[str, Tone]:
    """Картинки идут только модели запроса и модели зрения: маршрут не
    модели запроса — это «глаза»."""
    router = getattr(ctx, "model_router", None)
    if router is not None:
        main = router.resolve_main_at(
            getattr(state, "_call_level", None), check_breaker=False
        )
        if route_id(main) != route_id(resolved):
            return unsupported_note(note_alias(resolved), eyes_role=True)
    eyes = viewing_eyes(ctx, state, resolved)
    return unsupported_note(
        note_alias(resolved),
        viewer=note_alias(eyes) if eyes is not None else None,
    )
