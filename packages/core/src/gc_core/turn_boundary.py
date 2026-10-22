"""Граница хода: `begin_turn` — единственная точка, где начинается новый ход
главного цикла или субагента.

Вызывается из трёх мест: старт рана (`AgentExecutor.arun()`), подхват pending-очереди
как новый ход (`processing_responses._pickup_pending_as_new_turn`), старт субагента
(`SubagentRunner`). Модель выбирается перед каждым новым вызовом
(`processing_responses.resolve_main_for_call`). `state.default_model` — стартовая
модель для витрин и лога, далее её обновляет точка вызова.

В легаси-режиме (пустой реестр `models`) `resolve("main")` резолвит плоский `model`.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from gc_core.auto_routing import state_ops
from gc_core.auto_routing.levels import auto_active
from gc_core.execution.state import ExecutionState as State
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)


def begin_turn(state: State, ctx: Any) -> None:
    """Граница хода: сброс роутинга + стартовая модель.

    Уровень не закрепляется на ход: новый выбор применяется перед новым вызовом.
    `turn_level` очищается для снимков прежней версии; текущий вызов хранит
    свой уровень в `_call_level`, а повторы используют мемо решения такта.
    `state_ops.reset` — безусловно, первой строкой: сброс роутинга не зависит
    от наличия Router. Затем `state.default_model` из `ctx.model_router.resolve("main")`.

    Нет-op по модели, если у `ctx` нет `model_router` (тестовые двойники) —
    `default_model` остаётся тем, что задал вызывающий код.

    При активном auto (`levels.auto_active`) стартовая модель резолвится без
    предохранителя (`check_breaker=False`): уровень хода выбирает точка решения,
    она же заменяет мёртвый уровень ближайшим живым, а если мертвы все три —
    честно отказывает на первом вызове. В ручном режиме открытый алиас, как и
    раньше, поднимает `AliasCircuitOpenError` здесь, на старте хода.

    Здесь же снимается граница сообщений хода:
    свои сообщения ход в историю ещё не положил, а поставит её
    `State.mark_turn_start` на первом же батче."""
    attach = getattr(ctx, "attach_state", None)
    if callable(attach):
        attach(state)
    state_ops.reset(state)
    state.turn_level = None
    state.turn_messages_start = None
    if not getattr(state, "parent_state_id", None):
        # Ключ хода для проб зрения. В durable-исполнении id выдаёт среда
        # (``supplied_id``): ``uuid4`` в workflow недетерминирован.
        from gc_core.execution.environment import supplied_id

        state.vision_probe_turn = supplied_id("vision_turn") or str(uuid4())
        state.vision_probe_notices = set()
    router = getattr(ctx, "model_router", None)
    if router is None:
        return
    active = auto_active(
        router, router.live_level(), getattr(ctx, "auto_available", True)
    )
    state.default_model = router.resolve(
        "main", check_breaker=not active
    ).model


def opening_reply_id(state: Any) -> str | None:
    """``id`` реплики человека, открывшей текущий ход: первая нетехническая
    ``role: user`` от границы хода (``turn_messages_start``). Граница за
    концом истории — её переписала компакция, читается весь остаток.
    Границы нет (ход ничего не положил в историю) или реплики нет
    (континуация утверждённого плана) — ``None``."""
    start = getattr(state, "turn_messages_start", None)
    if start is None:
        return None
    messages = list(getattr(state, "messages", None) or [])
    if int(start) <= len(messages):
        messages = messages[int(start) :]
    for message in messages:
        if extract_real_user_message_text(message):
            mid = message.get("id")
            return mid if isinstance(mid, str) and mid else None
    return None


__all__ = ["begin_turn", "opening_reply_id"]
