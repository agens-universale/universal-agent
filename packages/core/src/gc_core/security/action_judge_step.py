"""Шаг проверки судьёй действий — один на все защиты режима auto.

Стадия режима, получив исход ``ALIGN``, делает ``yield from judged_step(...)``
и получает ``(разрешено, тело отказа)``. Здесь всё, что вокруг вердикта:
запомненные ответы человека, одна проверка на вызов, вызов модели с одним
повтором, счётчики, предохранитель и остановка прогона.

Предохранитель — как у эталона: 3 отказа подряд или 20 за сессию ставят
паузу; на паузе спорное действие спрашивается у человека обычным вопросом, а
спросить некого — отказ без вызова судьи, агент работает дальше. Счёт и пауза
живут в общем состоянии сессии (у субагентов оно то же) и снимаются «да»
человека на вопросе паузы или его новым сообщением. Сбои проверки и причина
остановки — в состоянии того агента, чей это вызов: у субагента они свои, и его
остановка не обрывает родителя.
"""

from __future__ import annotations

import platform
from collections.abc import Callable, Iterable, Mapping
from functools import partial
from typing import Any

from gc_core.execution.contracts import ExecutionFailure, ModelRequestStopped
from gc_core.execution.ports import port
from gc_core.message_policy.permission_denials import (
    ACTION_PAUSED_REASON,
    JUDGE_NO_CHANNEL_STOP_TEXT,
    action_blocked_error,
    judge_failures_stop_text,
    mode_denied_error,
)
from gc_core.message_policy.user_stop import cancelled_by_user_error
from gc_core.security.action_judge import (
    EXTERNAL_PROMPT,
    action_blocked,
    build_action_judge_messages,
    build_action_judge_payload,
    has_external_words,
    parse_action_verdict,
)
from gc_core.security.action_judge_input import build_judge_request
from gc_core.security.call_scope import current_call
from gc_core.security.command_moderation import judge_transport_configured
from gc_core.security.confirm_port import (
    confirm_available,
    confirmation_step,
    unconfirmed_error,
)
from gc_core.security.dual_wrapper import AwaitOnCaller
from gc_core.security.guard_probe import GuardProbeRequest, probe_step
from gc_core.security.path_policy import audit_policy_decision
from gc_core.settings import runtime_of, settings_of
from gc_core.summarize.llm import (
    arequest_auxiliary_completion_text,
    auxiliary_llm_judge_max_tokens,
)

DENIALS_IN_A_ROW = 3
DENIALS_TOTAL = 20
#: Строка вопроса на паузе: почему auto вдруг спрашивает человека.
PAUSE_QUESTION_FIELD = (
    "Почему вопрос",
    "проверка действий в режиме auto приостановлена после повторных отказов",
)
FAILURES_BEFORE_STOP = 10
#: Таймаут одного запроса к судье, с. Повторов клиента у судьи нет — только
#: свой один повтор (`_verdict_step`): до остановки по `FAILURES_BEFORE_STOP`
#: уходит не больше 20 запросов.
JUDGE_REQUEST_TIMEOUT_SEC = 60

CHECK_FAILED_TYPE = "action_check_failed"
#: Сбой проверки — не суждение о действии: модель не должна начать его избегать.
CHECK_FAILED_REASON = (
    "The automatic review did not complete; this says nothing about the "
    "action itself."
)
CHECK_FAILED_GUIDANCE = (
    "You may make this call again. Do not replace it with a riskier way of "
    "doing the same thing; if it fails again, continue with the rest of the "
    "task and report it to the user."
)


def lift_pause_on_user_message(shared: Any) -> None:
    """Паузу снимает решение человека: «да» на вопросе паузы или его новое
    сообщение. Сообщение снимает её всегда — и где спросить некого (сервер,
    A2A, `gc-cli` без терминала), и где «да» не придёт (клиент не умеет
    отвечать): человек прочитал «нужно решение человека» и написал дальше.
    Судья снова проверяет; упрётся агент опять — пауза встанет заново."""
    if shared.judge_paused:
        shared.judge_paused = False
        shared.judge_denials_in_a_row = 0


def judge_channel_configured(ctx: Any) -> bool:
    """Может ли этот прогон вообще позвать судью действий: есть ли канал
    вспомогательной модели. Это свойство конфигурации, а не временный сбой."""
    return judge_transport_configured(ctx)


async def _arequest_verdict_text(ctx: Any, state: Any, messages: list) -> str:
    return await arequest_auxiliary_completion_text(
        ctx=ctx,
        state=state,
        messages=messages,
        max_tokens=auxiliary_llm_judge_max_tokens(ctx),
        request_label="action_judge",
        # Тот же слот модели, что у судьи команд: отдельного ключа конфига нет.
        consumer="command_judge",
        # Свой бюджет: сбой судьи не отмечает модель основного хода
        # недоступной и не повторяется клиентом — повторяет и считает шаг.
        isolated_timeout_sec=JUDGE_REQUEST_TIMEOUT_SEC,
    )


def _check_failed(**fields: Any) -> dict:
    return mode_denied_error(
        CHECK_FAILED_TYPE,
        CHECK_FAILED_REASON,
        guidance=CHECK_FAILED_GUIDANCE,
        **fields,
    )


def _durable(ctx: Any) -> bool:
    """Durable-исполнение: ядру нельзя читать файлы и спрашивать систему —
    факты хоста и тексты приходят пробой с воркера."""
    return callable(port(ctx, "guard_probe"))


def _runtime(context: Any, extra: Mapping[str, Any] | None) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "project_dir": str(context.launch_root or ""),
    }
    if not _durable(context.ctx):
        facts["os"] = platform.system().lower()
    facts.update(extra or {})
    return facts


def judged_step(
    *,
    context: Any,
    tool_name: str,
    category: str,
    decision_key: str | None,
    question: tuple[str, str, list],
    flags: Iterable[str] = (),
    runtime: Mapping[str, Any] | Callable[[], Mapping[str, Any]] | None = None,
    error_fields: Mapping[str, Any] | None = None,
):
    """Генератор для ``yield from``: ``(разрешено, тело отказа | None)``."""
    ctx = context.ctx
    flags = frozenset(flags)
    error_fields = dict(error_fields or {})
    scope = current_call()
    audit = partial(_audit, context, tool_name, flags, scope)

    if scope is None or scope.state is None:
        # Вызов мимо диспетчера: судье нечего показать. Это сбой проверки,
        # а не пропуск.
        audit("blocked", "action_judge_no_call_scope")
        return False, _check_failed(**error_fields)
    state = scope.state
    if _already_allowed(scope, flags, ctx, decision_key, audit):
        return True, None
    if state.shared.judge_paused:
        return (
            yield from _paused_step(
                scope, flags, ctx, decision_key, question, error_fields, audit
            )
        )
    if not judge_channel_configured(ctx):
        state.judge_stop_reason = JUDGE_NO_CHANNEL_STOP_TEXT
        audit("blocked", "action_judge_no_channel")
        return False, _check_failed(**error_fields)

    # Факты запроса собираются только здесь: до этой точки судья мог и не
    # понадобиться, а часть фактов читает хост.
    payload = build_action_judge_payload(
        build_judge_request(
            state,
            ctx,
            tool=tool_name,
            category=category,
            arguments=scope.arguments,
            call_id=scope.call_id,
            tools_data=getattr(ctx, "tools_data", None) or {},
            flags=sorted(flags),
            runtime=_runtime(
                context, runtime() if callable(runtime) else runtime
            ),
            external_user_words=runtime_of(ctx).external_user_words,
        )
    )
    try:
        verdict = yield from _verdict_step(context, state, tool_name, payload)
    except ModelRequestStopped:
        # Стоп хода во время запроса к судье (durable отменил ожидание
        # модели): не сбой проверки и не отказ. Вызов закрывается телом
        # отказа-остановки, остаток батча — циклом по ``stop_requested``.
        audit("blocked", "action_judge_stopped")
        return False, cancelled_by_user_error(**error_fields)
    if verdict is None:
        state.judge_failures_in_a_row += 1
        if state.judge_failures_in_a_row >= FAILURES_BEFORE_STOP:
            state.judge_stop_reason = judge_failures_stop_text(
                FAILURES_BEFORE_STOP
            )
        audit("blocked", "action_judge_check_failed")
        return False, _check_failed(**error_fields)

    state.judge_failures_in_a_row = 0
    shared = state.shared
    if action_blocked(verdict, payload):
        _count_denial(shared)
        audit("blocked", "action_judge_blocked", verdict.reason)
        # «Прекрати пробовать» — только без человека: с ним следующая попытка
        # на паузе станет вопросом, и запрет пробовать этот вопрос бы съел.
        return False, action_blocked_error(
            verdict.reason,
            repeated=shared.judge_paused and not confirm_available(ctx),
            **error_fields,
        )
    shared.judge_denials_in_a_row = 0
    scope.judge_approved_flags = flags
    audit("executed", "action_judge_allowed", verdict.reason)
    return True, None


def _audit(
    context: Any,
    tool_name: str,
    flags: frozenset,
    scope: Any,
    outcome: str,
    reason_code: str,
    reason: str = "",
) -> None:
    audit_policy_decision(
        context,
        tool_name=tool_name,
        decision=outcome,
        reason_code=reason_code,
        risk_flags=sorted(flags),
        reason=reason or None,
        call_id=getattr(scope, "call_id", None),
    )


def _already_allowed(scope, flags, ctx, decision_key, audit) -> bool:
    """Судья не нужен: в этом вызове он уже разрешил то же или шире, либо
    человек разрешил это действие в сессии."""
    if (
        scope.judge_approved_flags is not None
        and flags <= scope.judge_approved_flags
    ):
        return True  # одна проверка на вызов
    store = getattr(ctx, "permission_store", None)
    if (
        decision_key
        and store is not None
        and store.decision_for(decision_key) is True
    ):
        audit("executed", "session_rule_allow")
        return True  # человек уже решил про это действие
    return False


def _paused_step(
    scope, flags, ctx, decision_key, question, error_fields, audit
):
    """Проверка на паузе: решает человек обычным вопросом этой защиты; без
    человека — отказ без судьи."""
    if not confirm_available(ctx):
        # Спросить некого: отказ без судьи — у агента, упёршегося в
        # проверку, больше нет попыток подобрать проходную формулировку.
        audit("blocked", "action_judge_paused")
        return False, action_blocked_error(
            ACTION_PAUSED_REASON, repeated=True, **error_fields
        )
    title, text, fields = question
    confirmed = yield confirmation_step(
        title=title,
        question=text,
        fields=[*fields, PAUSE_QUESTION_FIELD],
        ctx=ctx,
        decision_key=decision_key,
    )
    if not confirmed:
        audit("blocked", "action_judge_escalation_declined")
        return False, unconfirmed_error(ctx, **error_fields)
    shared = scope.state.shared
    shared.judge_paused = False
    shared.judge_denials_in_a_row = 0
    scope.judge_approved_flags = flags
    audit("executed", "action_judge_escalation_confirmed")
    return True, None


def _verdict_step(context, state, tool_name, payload):
    """Вердикт судьи с одним повтором; None — проверка не состоялась."""
    ctx = context.ctx
    tags = tuple(
        str(tag) for tag in settings_of(ctx).strip_tags_projection.tags
    )
    messages = None
    for _attempt in range(2):  # один повтор
        try:
            if messages is None:
                messages = yield from _judge_messages_step(
                    context, tool_name, payload
                )
            raw = yield AwaitOnCaller(
                partial(_arequest_verdict_text, ctx, state, messages)
            )
            return parse_action_verdict(raw, tags=tags)
        except (ExecutionFailure, ModelRequestStopped):
            # Отказ исполнения и остановка хода — не обратная связь модели и
            # не повод для повтора: отказ разбирает цикл, остановку —
            # ``judged_step`` (тело отказа-остановки).
            raise
        except Exception:  # негодный ответ или сбой запроса — не состоялась
            continue
    return None


def _judge_messages_step(context, tool_name, payload):
    """Сообщения судье; под durable инструкции читает хост. Правило о
    внешнем запросе проба приносит, только если шаг его попросил."""
    prompts = None
    if _durable(context.ctx):
        facts = yield probe_step(
            context.ctx,
            GuardProbeRequest(
                "action_prompts",
                tool_name,
                EXTERNAL_PROMPT if has_external_words(payload) else "",
                str(context.launch_root or ""),
            ),
        )
        prompts = facts.data
    return build_action_judge_messages(payload, prompts=prompts)


def _count_denial(shared: Any) -> None:
    """Отказ судьи в общий счёт; пауза — после трёх подряд или двадцати за
    сессию."""
    shared.judge_denials_in_a_row += 1
    shared.judge_denials_total += 1
    if shared.judge_denials_total >= DENIALS_TOTAL:
        # Общий счёт сбрасывается, только когда сработал сам.
        shared.judge_denials_total = 0
        shared.judge_paused = True
    if shared.judge_denials_in_a_row >= DENIALS_IN_A_ROW:
        shared.judge_paused = True
