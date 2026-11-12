"""Вызов судьи начальной пары хода через ресурсный и модельный порты.

Сбой судьи выбирает сложную пару и записывает причину в лог. Нарушения
контракта durable-рантайма проходят наружу, чтобы не скрывать сбой workflow.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any

from gc_core.agent_store import (
    AgentStore,
    Prompt,
    agent_store_of,
    aread_required,
)
from gc_core.auto_routing.log_events import log_routing_pair_judge
from gc_core.auto_routing.pair_judge import (
    SOURCE_FALLBACK,
    SOURCE_JUDGE,
    PairChoice,
    build_pair_card,
    parse_pair_score,
)
from gc_core.auto_routing.pair_judge import (
    prompt_path as pair_prompt_path,
)
from gc_core.auto_routing.policy import (
    PAIR_FALLBACK_LEVEL,
    RoutingParams,
    pair_levels,
    pair_safe_level,
)
from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.ports import is_sandbox_violation, port
from gc_core.execution.resources import (
    PromptResourceRequest,
    aload_prompt_resource,
)
from gc_core.runtime_notices import maybe_push_pair_prompt_notice
from gc_core.summarize.llm import (
    arequest_auxiliary_completion_text,
    auxiliary_llm_judge_max_tokens,
)

_logger = logging.getLogger(__name__)

CONSUMER = "routing_judge"
#: Метка судьи пары: учёт остаётся в категории routing_judge.
PAIR_REQUEST_LABEL = "routing_judge_pair"


async def aload_judge_prompt(store: AgentStore) -> str:
    """Инструкция судьи пары (у файлового хранилища —
    ``paths.chat_completions_routing_pair_judge_prompt_path`` или пакетная)."""
    label = store.label("prompt", Prompt.ROUTING_PAIR_JUDGE)
    prompt = (
        await aread_required(
            store,
            "prompt",
            Prompt.ROUTING_PAIR_JUDGE,
            missing=f"Routing judge prompt file not found: {label}",
        )
    ).strip()
    if not prompt:
        raise ValueError(f"Routing judge prompt file is empty: {label}")
    return prompt


async def arun_pair_judge(
    state: Any, ctx: Any, *, params: RoutingParams, mid_turn: bool
) -> PairChoice:
    """Один вызов судьи пары. Возвращает выбор пары хода; любой сбой — сложная пара
    (`PAIR_FALLBACK_LEVEL`) с причиной. Исключений наружу не выпускает: ход не имеет
    права падать из-за судьи."""
    logger = getattr(ctx, "logger", None) or _logger
    operations = port(ctx, "operations")
    clock = operations.monotonic if operations is not None else time.monotonic
    started = clock()
    card = ""
    choice = PairChoice(PAIR_FALLBACK_LEVEL, None, "", SOURCE_FALLBACK, None)
    try:
        card = build_pair_card(
            state, legacy=not getattr(ctx, "routing_contract_v2", True)
        )
        try:
            system_prompt = await aload_prompt_resource(
                ctx,
                PromptResourceRequest("routing_pair_judge"),
                local=lambda: aload_judge_prompt(agent_store_of(ctx)),
            )
        except (FileNotFoundError, ValueError) as exc:
            path = pair_prompt_path(ctx)
            logger.warning(
                f"routing pair judge prompt unusable: {path}: {exc}"
            )
            maybe_push_pair_prompt_notice(
                path=path,
                reason=str(exc),
                enabled=bool(getattr(ctx, "is_tui", False)),
            )
            raise
        raw = await arequest_auxiliary_completion_text(
            ctx=ctx,
            state=state,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": card},
            ],
            max_tokens=auxiliary_llm_judge_max_tokens(ctx),
            request_label=PAIR_REQUEST_LABEL,
            consumer=CONSUMER,
        )
        score, reason, error = parse_pair_score(raw)
        if score is None:
            choice = replace(choice, error=error)
        else:
            choice = PairChoice(
                pair_safe_level(score, params.pair_threshold),
                score,
                reason,
                SOURCE_JUDGE,
                None,
            )
    except ExecutionFailure:
        raise
    except Exception as exc:  # судья пары не имеет права ронять ход
        if is_sandbox_violation(exc):
            raise
        choice = replace(choice, error=f"{type(exc).__name__}: {exc}".strip())
    log_routing_pair_judge(
        logger,
        turn=int(getattr(state, "user_turn_seq", 0) or 0) + 1,
        mid_turn=mid_turn,
        choice=choice,
        pair_threshold=params.pair_threshold,
        pair=pair_levels(replace(params, safe_level=choice.safe_level)),
        card_chars=len(card),
        latency_ms=int((clock() - started) * 1000),
    )
    return choice
