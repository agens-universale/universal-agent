"""Фиксация модели на ход (спека §6.4, Task 5 плана «Профили v2, волна A»).

``begin_turn`` — единственная точка, где ``state.default_model`` пересчитывается
через ``ctx.model_router.resolve("main")``. Вызывается из обеих точек начала хода:
- старт рана — ``AgentExecutor.run()`` (``src/agent_executor.py``);
- подхват pending-сообщения как новый ход — ``processing_responses.run_agent``
  (после ``state.flush_current_user_intent()``, когда очередь непуста).

Внутри хода модель не перечитывается — ``state.default_model`` меняется только здесь.

В легаси-режиме (пустой реестр ``models``) ``ModelRouter.resolve("main")`` резолвит
плоский ``model`` из снимка конфига — поведение не отличается от до-Router кода.
"""

from __future__ import annotations

from typing import Any

from gc_core.execution.state import ExecutionState as State


def begin_turn(state: State, ctx: Any) -> None:
    """Снимает ``ctx.model_router.resolve("main")`` и фиксирует ``state.default_model``
    вместе с уровнем хода (``state.turn_level``).

    Уровень фиксируется отдельной строкой, потому что фиксации одного
    ``default_model`` мало: воронка запроса получает весь ``ResolvedModel``
    (url/токен/квирки), и без пина уровня она резолвила бы его заново на КАЖДЫЙ
    запрос — ``/level`` переключается без пересборки executor (единственный такой
    тоггл), и смена посреди хода уводила бы запросы на другую модель прямо в
    середине цикла tool-call'ов. Тот же приём, что у дочернего Router'а субагента
    (``SubagentRunner._build_child_model_router`` замораживает level_provider
    константой) — читает пин ``processing_responses._resolve_main_for_turn``.

    Нет-op, если у ``ctx`` нет ``model_router`` (тестовые двойники контекста, не
    оперирующие Router'ом) — ``state.default_model`` остаётся тем, что задал
    вызывающий код.
    """
    router = getattr(ctx, "model_router", None)
    if router is None:
        return
    state.turn_level = router.active_level()
    state.default_model = router.resolve("main").model


__all__ = ["begin_turn"]
