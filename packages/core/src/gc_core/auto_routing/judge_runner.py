"""Вызов судьи начальной пары хода через ресурсный и модельный порты.

Сбой судьи выбирает сложную пару и записывает причину в лог. Нарушения
контракта durable-рантайма проходят наружу, чтобы не скрывать сбой workflow.
"""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import Any, Optional

from gc_core.agent_store import (
    DEFAULT_AGENT_STORE, AgentResource, AgentStore, agent_store_of, read_required_text,
)
from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.ports import is_sandbox_violation, port
from gc_core.execution.resources import PromptResourceRequest, aload_prompt_resource
from gc_core.auto_routing.log_events import log_routing_pair_judge
from gc_core.auto_routing.pair_judge import (
    PairChoice, SOURCE_FALLBACK, SOURCE_JUDGE,
    build_pair_card, parse_pair_score, prompt_path as pair_prompt_path, default_pair_judge_prompt_path,
)
from gc_core.auto_routing.policy import (
    PAIR_FALLBACK_LEVEL, RoutingParams, pair_levels, pair_safe_level,
)
from gc_core.settings import settings_of
from gc_core.summarize.llm import (
    arequest_auxiliary_completion_text,
    auxiliary_llm_judge_max_tokens,
)

_logger = logging.getLogger(__name__)

CONSUMER = "routing_judge"
#: Метка судьи пары: учёт остаётся в категории routing_judge.
PAIR_REQUEST_LABEL = "routing_judge_pair"


def _load_judge_prompt(
    prompt_path: Optional[str] = None,
    *,
    store: AgentStore = DEFAULT_AGENT_STORE,
) -> str:
    path = prompt_path or default_pair_judge_prompt_path()
    prompt = read_required_text(
        store, AgentResource("prompt", "routing_pair_judge", path),
        missing=f"Routing judge prompt file not found: {path}",
    ).strip()
    if not prompt:
        raise ValueError(f"Routing judge prompt file is empty: {path}")
    return prompt


async def arun_pair_judge(state: Any, ctx: Any, *, params: RoutingParams, mid_turn: bool) -> PairChoice:
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
        card = build_pair_card(state)
        system_prompt = await aload_prompt_resource(
            ctx, PromptResourceRequest(
                "routing_pair_judge",
                path=settings_of(ctx).paths.chat_completions_routing_pair_judge_prompt_path,
            ),
            local=lambda: _load_judge_prompt(
                pair_prompt_path(ctx), store=agent_store_of(ctx),
            ),
        )
        raw = await arequest_auxiliary_completion_text(
            ctx=ctx,
            state=state,
            messages=[{"role": "system",
                       "content": system_prompt},
                      {"role": "user", "content": card}],
            max_tokens=auxiliary_llm_judge_max_tokens(ctx),
            request_label=PAIR_REQUEST_LABEL,
            consumer=CONSUMER,
        )
        score, reason, error = parse_pair_score(raw)
        if score is None:
            choice = replace(choice, error=error)
        else:
            choice = PairChoice(pair_safe_level(score, params.pair_threshold), score, reason,
                                SOURCE_JUDGE, None)
    except ExecutionFailure:
        raise
    except Exception as exc:                       # судья пары не имеет права ронять ход
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
