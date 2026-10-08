from __future__ import annotations

import inspect
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from gc_core.agent_store import (
    DEFAULT_AGENT_STORE,
    AgentResource,
    AgentStore,
    agent_store_of,
)
from gc_core.bundled_assets import bundled_root
from gc_core.config_modules.security import validate_command_moderation_mode
from gc_core.constants.tool_names import EXEC_PYTHON_NAME
from gc_core.execution.contracts import ExecutionFailure
from gc_core.execution.ports import port
from gc_core.message_projection import project_text_for_model
from gc_core.security.confirm_port import (
    confirm_available,
    confirmation_step,
    request_confirm,
)
from gc_core.security.dual_wrapper import AwaitOnCaller, OffLoop, dual_wrap
from gc_core.security.guard_probe import GuardProbeRequest, probe_step
from gc_core.security.path_policy import (
    ToolSecurityContext,
    _settings_source,
    audit_policy_decision,
    resolve_tool_context,
)
from gc_core.security.protected_paths import protected_paths
from gc_core.settings import settings_of
from gc_core.summarize.llm import (
    arequest_auxiliary_completion_text,
    auxiliary_llm_judge_max_tokens,
)
from gc_core.utils.project_data import os_label

SAFE_VERDICT = "safe"
DANGEROUS_VERDICT = "potentially_dangerous"
ALLOWED_VERDICTS = {SAFE_VERDICT, DANGEROUS_VERDICT}

#: Инструкция агенту при любом отказе выполнить команду.
#:
#: Судья оценивает КОНКРЕТНУЮ КОМАНДУ, а не намерение, и модель легко переписывает
#: её в эквивалент: заблокированное `rm -rf victim` превращается в
#: `rm victim/a.txt && rmdir victim`, которое судья пропускает как безопасное.
#: Наблюдалось на живом прогоне 17.08.2026 — каталог был удалён.
#:
#: Раньше этот текст лежал только в ветках «пользователь отменил» и был
#: продублирован в трёх местах, а до ветки «судья заблокировал» не доезжал вовсе.
#: Теперь формулировка одна и уходит в конверт отдельным полем `guidance`
#: (см. make_block_error), чтобы не смешиваться с причинами судьи в `reason` —
#: `reason` показывается пользователю в диалоге подтверждения.
NO_WORKAROUND_NOTICE = (
    "Do NOT retry the same command, and do NOT run a rewritten, split-up or otherwise "
    "equivalent command that reaches the same result (different flags, another tool, "
    "several smaller steps) — report the block to the user and ask how to proceed."
)
_VERDICT_SAFE_RE = re.compile(
    r'"verdict"\s*:\s*"safe"\s*[,}]',
    re.IGNORECASE,
)
_VERDICT_DANGEROUS_RE = re.compile(
    r'"verdict"\s*:\s*"(?:potentially_dangerous|dangerous|unsafe)"\s*[,}]',
    re.IGNORECASE,
)
_CONFIDENCE_NOT_NUMERIC = "judge result confidence must be numeric"


def effective_command_cwd(
    tool_name: str,
    kwargs: dict[str, Any],
    context: ToolSecurityContext,
) -> str:
    """
    Canonical working directory shown to the command judge and used by tools that honour it.

    Mirrors default cwd resolution for moderated tools so the judge matches subprocess execution.
    """
    cwd_arg = kwargs.get("cwd")
    if isinstance(cwd_arg, str) and cwd_arg.strip():
        return os.path.abspath(cwd_arg.strip())
    if tool_name == EXEC_PYTHON_NAME:
        return os.path.abspath(str(context.launch_root))
    return os.path.abspath(os.getcwd())


@dataclass(frozen=True)
class ModerationContext:
    tool_name: str
    launch_root: str
    extra_paths: list[str] = field(default_factory=list)
    mode: str = "deny"
    cwd: str = field(default_factory=os.getcwd)
    os_name: str = field(default_factory=os_label)
    shell: str = ""
    has_venv: bool = False
    allowed_command_examples: list[str] = field(default_factory=list)
    denied_command_examples: list[str] = field(default_factory=list)
    insession_examples: dict[str, list[str]] = field(
        default_factory=lambda: {"allowed": [], "denied": []}
    )
    prepared_never_touch_paths: list[str] | None = None


@dataclass(frozen=True)
class JudgeResult:
    verdict: str
    reasons: list[str]
    risk_flags: list[str]
    confidence: float


@dataclass(frozen=True)
class ExecutionDecision:
    allowed: bool
    decision: str
    reason_code: str
    reason: str
    risk_flags: list[str] = field(default_factory=list)
    error_type: str | None = None
    requires_confirmation: bool = False


class JudgeUnavailable(RuntimeError):
    pass


def _get_protected_paths(cwd: str | None):
    return protected_paths(cwd=cwd)


def load_never_touch_paths(cwd: str | None) -> list[str]:
    """Snapshot host paths with the command judge's existing local fallback."""
    try:
        paths = _get_protected_paths(cwd)
        return sorted(
            str(path) for path in paths.instructions | paths.operational
        )
    except Exception:
        return []


def build_judge_payload(
    tool_name: str,
    payload: str,
    context: ModerationContext,
) -> dict[str, Any]:
    never_touch = (
        load_never_touch_paths(context.cwd)
        if context.prepared_never_touch_paths is None
        else list(context.prepared_never_touch_paths)
    )
    return {
        "tool_name": tool_name,
        "payload": payload,
        "launch_root": context.launch_root,
        "allowed_write_paths": [
            context.launch_root,
            *context.extra_paths,
        ],
        "runtime": {
            "cwd": context.cwd,
            "os": context.os_name,
            "shell": context.shell,
            "has_venv": context.has_venv,
        },
        "allowed_command_examples": context.allowed_command_examples,
        "denied_command_examples": context.denied_command_examples,
        "insession_examples": context.insession_examples,
        "never_touch_paths": never_touch,
    }


def run_command_judge(
    judge_client: Any, judge_payload: dict[str, Any]
) -> JudgeResult:
    if judge_client is None:
        raise JudgeUnavailable("judge client is not configured")
    raw_result = (
        judge_client(judge_payload) if callable(judge_client) else judge_client
    )
    if isinstance(raw_result, JudgeResult):
        return raw_result
    return validate_judge_result(raw_result)


def judge_with_retry_flow(
    judge_client: Any, judge_payload: dict[str, Any], *, retries: int = 1
):
    """То же правило ретраев, но вызов судьи отдан ДРАЙВЕРУ гварда.

    Тело гварда делегирует сюда через `yield from`: `AwaitOnCaller` уводит
    корутину судьи на loop вызывающего (соединение и пул общие с агентом),
    `OffLoop` — синхронного судью в поток. Отказ прилетает обратно в точку
    `yield` через `throw`, поэтому `try/except` здесь работает ровно так же,
    как вокруг прямого вызова в синхронном близнеце.
    """
    if judge_client is None:
        raise JudgeUnavailable("judge client is not configured")
    request = (
        AwaitOnCaller if inspect.iscoroutinefunction(judge_client) else OffLoop
    )
    last_exc: Exception | None = None
    for _ in range(retries + 1):
        try:
            if request is AwaitOnCaller:
                raw = yield AwaitOnCaller(lambda: judge_client(judge_payload))
            else:
                raw = yield OffLoop(judge_client, judge_payload)
            if isinstance(raw, JudgeResult):
                return raw
            return validate_judge_result(raw)
        except ExecutionFailure:
            raise
        except Exception as exc:
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def validate_judge_result(raw: dict[str, Any]) -> JudgeResult:
    if not isinstance(raw, dict):
        raise ValueError("judge result must be an object")
    # Нормализуем ФОРМАТ (пробел/дефис/регистр): слабая модель часто пишет
    # «potentially dangerous» через пробел/в другом регистре. Синонимы НЕ маппим —
    # неузнанный вердикт по-прежнему отвергается (security не ослаблен).
    verdict = re.sub(
        r"[\s\-]+", "_", str(raw.get("verdict", "")).strip().lower()
    )
    if verdict not in ALLOWED_VERDICTS:
        raise ValueError(
            "judge result verdict must be 'safe' or 'potentially_dangerous'"
        )
    reasons = _string_list(raw.get("reasons"), field_name="reasons")
    risk_flags = _string_list(raw.get("risk_flags"), field_name="risk_flags")
    if verdict == DANGEROUS_VERDICT and not risk_flags:
        raise ValueError(
            "potentially_dangerous verdict requires at least one risk flag"
        )
    confidence_float = _parse_judge_confidence(raw.get("confidence", 0.0))
    return JudgeResult(
        verdict=verdict,
        reasons=reasons,
        risk_flags=risk_flags,
        confidence=confidence_float,
    )


#: Промежуточное решение режима deny при недоступном судье: нужен ответ
#: ЧЕЛОВЕКА. Отдельный код — чтобы драйвер не подставил вместо человека
#: экстрактор подтверждения из аргументов вызова, то есть от модели.
JUDGE_UNAVAILABLE_CONFIRM_REQUIRED = "judge_unavailable_confirm_required"


def _decide_without_judge(
    normalized_mode: str, user_confirmation: bool | None, ctx: Any
) -> ExecutionDecision:
    if normalized_mode == "ask":
        if user_confirmation is True:
            return ExecutionDecision(
                allowed=True,
                decision="executed",
                reason_code="user_confirmation",
                reason="User confirmed execution after judge was unavailable.",
            )
        if user_confirmation is False:
            return ExecutionDecision(
                allowed=False,
                decision="cancelled",
                reason_code="user_confirmation",
                reason="User declined the command.",
                error_type="unsafe_command_cancelled_by_user",
            )
        return ExecutionDecision(
            allowed=False,
            decision="asked",
            reason_code="judge_unavailable",
            reason="Judge is unavailable; user confirmation is required.",
            requires_confirmation=True,
        )
    # C1 Часть 2: при недоступности судьи в deny — спросить через confirm-порт.
    #
    # Сама функция человека НЕ спрашивает: решение здесь чистое, а ожидание
    # человека — дело драйвера гарда (`yield OffLoop(...)` в теле ниже).
    # Раньше `request_confirm` звался прямо отсюда, а функцию тело гарда
    # зовёт инлайн — в асинхронном пути ожидание человека (в TUI до 600 с)
    # шло прямо на event loop агента. Протокол теперь двухшаговый, как у
    # режима ask: сначала «нужно подтверждение», потом решение по ответу.
    if user_confirmation is True:
        return ExecutionDecision(
            allowed=True,
            decision="executed",
            reason_code="judge_unavailable_user_confirmed",
            reason="User confirmed execution after judge was unavailable.",
        )
    if user_confirmation is None and confirm_available(ctx):
        return ExecutionDecision(
            allowed=False,
            decision="asked",
            reason_code=JUDGE_UNAVAILABLE_CONFIRM_REQUIRED,
            reason="Judge is unavailable; user confirmation is required.",
            requires_confirmation=True,
        )
    # Fail-closed: confirm недоступен или пользователь отказал — блокируем.
    return ExecutionDecision(
        allowed=False,
        decision="blocked",
        reason_code="judge_unavailable",
        reason="Judge is unavailable; fail-closed policy blocks execution.",
        error_type="unsafe_command_blocked",
    )


def decide_command_execution(
    mode: str,
    judge_result: JudgeResult | None,
    judge_available: bool,
    user_confirmation: bool | None,
    *,
    ctx: Any = None,
) -> ExecutionDecision:
    normalized_mode = validate_command_moderation_mode(mode)
    if normalized_mode == "allow":
        return ExecutionDecision(
            allowed=True,
            decision="executed",
            reason_code="policy_allow",
            reason="command_moderation.mode=allow",
        )
    if not judge_available or judge_result is None:
        return _decide_without_judge(normalized_mode, user_confirmation, ctx)
    if judge_result.verdict == SAFE_VERDICT:
        return ExecutionDecision(
            allowed=True,
            decision="executed",
            reason_code="judge_verdict",
            reason=_join_reasons(
                judge_result.reasons, default="Judge marked payload as safe."
            ),
            risk_flags=judge_result.risk_flags,
        )
    if normalized_mode == "deny":
        return ExecutionDecision(
            allowed=False,
            decision="blocked",
            reason_code="judge_verdict",
            reason=_join_reasons(
                judge_result.reasons,
                default="Judge marked payload as dangerous.",
            ),
            risk_flags=judge_result.risk_flags,
            error_type="unsafe_command_blocked",
        )
    if user_confirmation is True:
        return ExecutionDecision(
            allowed=True,
            decision="executed",
            reason_code="user_confirmation",
            reason="User confirmed potentially dangerous execution.",
            risk_flags=judge_result.risk_flags,
        )
    if user_confirmation is False:
        return ExecutionDecision(
            allowed=False,
            decision="cancelled",
            reason_code="user_confirmation",
            reason="User declined the command.",
            risk_flags=judge_result.risk_flags,
            error_type="unsafe_command_cancelled_by_user",
        )
    return ExecutionDecision(
        allowed=False,
        decision="asked",
        reason_code="judge_verdict",
        reason=_join_reasons(
            judge_result.reasons, default="User confirmation is required."
        ),
        risk_flags=judge_result.risk_flags,
        requires_confirmation=True,
    )


def make_block_error(
    decision: ExecutionDecision,
    mode: str,
) -> dict[str, Any]:
    """Tool-result error for a blocked/cancelled command.

    No command/code preview: the model already has the full payload in the
    preceding function_call arguments; duplicating a truncated copy wastes tokens.
    """
    error = {
        "type": decision.error_type or "unsafe_command_blocked",
        "mode": mode,
        "reason": decision.reason,
        # Единственная воронка всех отказов: к этому месту решение уже финальное
        # (раунд подтверждения отыгран выше по коду). Поэтому запрет на обход
        # кладём здесь — его получают все ветки, включая «судья заблокировал»,
        # которая раньше уходила к модели без единой поведенческой инструкции.
        # Отдельным полем, а не приклейкой к reason: reason несёт причины судьи
        # и показывается пользователю в диалоге подтверждения.
        "guidance": NO_WORKAROUND_NOTICE,
        "risk_flags": decision.risk_flags,
    }
    return {key: value for key, value in error.items() if value is not None}


def moderate_command_execution(
    payload_arg: str | None = None,
    payload_extractor: Callable[..., str] | None = None,
    tool_name: str = "",
    judge_client_extractor: Callable[..., Any] | None = None,
    user_confirmation_extractor: Callable[..., bool | None] | None = None,
    blocked_result_factory: Callable[[dict[str, Any]], str] | None = None,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    def decorator(func: Callable[..., str]) -> Callable[..., str]:
        # Тело — ГЕНЕРАТОР: `dual_wrap` строит по нему обе обёртки. Обычный
        # декоратор на `async def acall` ломался молча — в ветке отказа отдавал
        # строку вместо корутины. `return (yield)` — точка вызова самого тула.
        #
        # Тело идёт ИНЛАЙН: блокирующего в нём самом не осталось. Судью оно
        # просит у драйвера через `AwaitOnCaller` (корутина ждётся на loop
        # вызывающего, на общем пуле соединений), ожидание человека — через
        # `OffLoop` (свой поток). Раньше в поток уезжало тело ЦЕЛИКОМ, и
        # судья звался оттуда через `run_coro_blocking`: свой `asyncio.run`,
        # свой пул httpx и новое TLS-рукопожатие на каждый `exec_command`.
        def _flow(*args: Any, **kwargs: Any):
            context = resolve_tool_context(*args, **kwargs)
            # Модерация выключена (``security.command_moderation.enabled``) —
            # тул идёт без судьи. Умолчание модели — включена
            # (secure-by-default).
            _source = _settings_source(
                args[0] if args else None, getattr(context, "ctx", None)
            )
            if not settings_of(_source).security.command_moderation.enabled:
                return (yield)
            try:
                payload = _extract_payload(
                    func, args, kwargs, payload_arg, payload_extractor
                )
            except ValueError:
                return (yield)
            host_facts = None
            if callable(port(context.ctx, "guard_probe")):
                host_facts = yield probe_step(
                    context.ctx,
                    GuardProbeRequest(
                        "command_host",
                        tool_name or func.__name__,
                        str(kwargs.get("cwd") or ""),
                        context.launch_root,
                    ),
                )
            moderation_context = _build_context(
                tool_name or func.__name__,
                context,
                kwargs,
                host_facts=host_facts,
            )
            if moderation_context.mode == "allow":
                audit_policy_decision(
                    context,
                    tool_name=moderation_context.tool_name,
                    decision="executed",
                    reason_code="policy_allow",
                )
                return (yield)

            judge_result = None
            judge_available = True
            try:
                judge_client = (
                    judge_client_extractor(*args, **kwargs)
                    if judge_client_extractor is not None
                    else _default_judge_client(args, kwargs)
                )
                judge_result = yield from judge_with_retry_flow(
                    judge_client,
                    build_judge_payload(
                        moderation_context.tool_name,
                        payload,
                        moderation_context,
                    ),
                    retries=1,
                )
            except ExecutionFailure:
                raise
            except Exception as exc:
                judge_available = False
                _log_command_judge_failure(
                    context,
                    tool_name=moderation_context.tool_name,
                    exc=exc,
                )

            tool_ctx = getattr(context, "ctx", None)
            decision = decide_command_execution(
                moderation_context.mode,
                judge_result,
                judge_available,
                None,
                ctx=tool_ctx,
            )
            if decision.requires_confirmation:
                audit_policy_decision(
                    context,
                    tool_name=moderation_context.tool_name,
                    decision="asked",
                    reason_code=decision.reason_code,
                    risk_flags=decision.risk_flags,
                    error_type=decision.error_type,
                )
                user_confirmation = yield from _obtain_user_confirmation(
                    user_confirmation_extractor,
                    payload,
                    moderation_context,
                    decision,
                    tool_ctx,
                    args,
                    kwargs,
                )
                decision = decide_command_execution(
                    moderation_context.mode,
                    judge_result,
                    judge_available,
                    user_confirmation,
                    ctx=tool_ctx,
                )
            if decision.allowed:
                audit_policy_decision(
                    context,
                    tool_name=moderation_context.tool_name,
                    decision=decision.decision,
                    reason_code=decision.reason_code,
                    risk_flags=decision.risk_flags,
                    error_type=decision.error_type,
                )
                return (yield)
            error = make_block_error(decision, moderation_context.mode)
            audit_policy_decision(
                context,
                tool_name=moderation_context.tool_name,
                decision=decision.decision,
                reason_code=decision.reason_code,
                risk_flags=decision.risk_flags,
                error_type=error.get("type"),
            )
            if blocked_result_factory is not None:
                return blocked_result_factory(error)
            tool_blocked_result = _tool_blocked_result(args)
            if callable(tool_blocked_result):
                return tool_blocked_result(error, *args[1:], **kwargs)
            return json.dumps(
                {
                    "tool": moderation_context.tool_name,
                    "ok": False,
                    "error": error,
                },
                ensure_ascii=False,
            )

        return dual_wrap(func, _flow)

    return decorator


def _obtain_user_confirmation(
    user_confirmation_extractor: Callable[..., bool | None] | None,
    payload: str,
    moderation_context: ModerationContext,
    decision: ExecutionDecision,
    tool_ctx: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
):
    """Ответ на «нужно подтверждение» — генератор, звать через `yield from`."""
    # Экстрактор — только для режима ask. В deny подтверждение даёт
    # человек через порт: иначе подтверждение из аргументов вызова
    # (то есть от модели) разрешало бы команду при недоступном судье.
    if (
        user_confirmation_extractor is not None
        and decision.reason_code != JUDGE_UNAVAILABLE_CONFIRM_REQUIRED
    ):
        return user_confirmation_extractor(*args, **kwargs)
    return (
        yield confirmation_step(
            title="Проверка команды",
            question=f"Разрешить запуск? ({moderation_context.tool_name})",
            fields=[
                (
                    _primary_payload_label(moderation_context.tool_name),
                    payload,
                ),
                ("Причина", decision.reason),
                ("Риски", ", ".join(decision.risk_flags) or "-"),
            ],
            ctx=tool_ctx,
            decision_key=f"command:{payload}",
            local_wait=lambda: _ask_user_confirmation(
                payload,
                moderation_context,
                decision,
                tool_ctx,
            ),
        )
    )


def _build_context(
    tool_name: str,
    context: ToolSecurityContext,
    kwargs: dict[str, Any],
    *,
    host_facts: Any = None,
) -> ModerationContext:
    cwd_value = (
        host_facts.canonical_path
        if host_facts is not None
        else effective_command_cwd(tool_name, kwargs, context)
    )
    insession_examples: dict[str, list[str]] = {"allowed": [], "denied": []}
    permission_store = getattr(context.ctx, "permission_store", None)
    if permission_store is not None:
        try:
            insession_examples = permission_store.insession_examples()
        except Exception:
            insession_examples = {"allowed": [], "denied": []}
    return ModerationContext(
        tool_name=tool_name,
        launch_root=context.launch_root,
        extra_paths=context.write_guard.extra_paths,
        mode=context.command_moderation_mode,
        cwd=str(cwd_value),
        os_name=host_facts.os_name if host_facts is not None else os_label(),
        shell=(
            host_facts.shell
            if host_facts is not None
            else os.environ.get("SHELL") or os.environ.get("COMSPEC") or ""
        ),
        has_venv=(
            host_facts.has_venv
            if host_facts is not None
            else bool(os.environ.get("VIRTUAL_ENV"))
        ),
        allowed_command_examples=context.allowed_command_examples,
        denied_command_examples=context.denied_command_examples,
        insession_examples=insession_examples,
        prepared_never_touch_paths=(
            host_facts.data.get("never_touch_paths")
            if host_facts is not None
            else None
        ),
    )


def _extract_payload(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    payload_arg: str | None,
    payload_extractor: Callable[..., str] | None,
) -> str:
    if payload_extractor is not None:
        return payload_extractor(*args, **kwargs)
    effective_payload_arg = payload_arg or "payload"
    bound = inspect.signature(func).bind_partial(*args, **kwargs)
    payload = bound.arguments.get(effective_payload_arg)
    if not isinstance(payload, str):
        raise ValueError(f"{effective_payload_arg} must be a string")
    return payload


def _log_command_judge_failure(
    context: ToolSecurityContext,
    *,
    tool_name: str,
    exc: BaseException,
) -> None:
    logger = context.logger
    if logger is None or not hasattr(logger, "warning"):
        return
    event = {
        "event": "command_moderation_judge_failed",
        "tool_name": tool_name,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    try:
        logger.warning(
            "command_moderation_judge_failed %s",
            json.dumps(event, ensure_ascii=False, sort_keys=True),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    except Exception:
        return


def _parse_judge_confidence(raw: Any) -> float:
    if isinstance(raw, (int, float)):
        confidence_float = float(raw)
    elif isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            raise ValueError(_CONFIDENCE_NOT_NUMERIC)
        try:
            confidence_float = float(stripped)
        except ValueError as exc:
            raise ValueError(_CONFIDENCE_NOT_NUMERIC) from exc
    else:
        raise ValueError(_CONFIDENCE_NOT_NUMERIC)
    if confidence_float < 0.0 or confidence_float > 1.0:
        raise ValueError("judge result confidence must be between 0.0 and 1.0")
    return confidence_float


def _string_list(raw: Any, *, field_name: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(
        isinstance(item, str) for item in raw
    ):
        raise ValueError(
            f"judge result {field_name} must be a list of strings"
        )
    return raw


def _join_reasons(reasons: list[str], *, default: str) -> str:
    return "; ".join(reason for reason in reasons if reason) or default


def _default_judge_client(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> Callable[[dict[str, Any]], Any]:
    """Судья модерации: корутина, которую ждёт loop ВЫЗЫВАЮЩЕГО.

    Прежняя редакция была синхронной и поднимала `asyncio.run` на каждый
    вызов — свежий пул httpx и новое TLS-рукопожатие к LLM на КАЖДЫЙ
    модерируемый `exec_command`, при том что соединение агента простаивало
    рядом. Шов остался один и под тем же именем: `judge_with_retry_flow`
    сама смотрит, корутинный ей отдали судью или обычный, поэтому подмена
    синхронной заглушкой в тестах работает по-прежнему.
    """
    self_obj = args[0] if args else None
    ctx = kwargs.get("ctx") or getattr(self_obj, "_ctx", None)
    state = kwargs.get("state") or getattr(self_obj, "_state", None)
    settings = settings_of(_settings_source(self_obj, ctx))
    configured = getattr(ctx, "model_transport_configured", None)
    if configured is None:
        configured = bool(settings.backend.completions_url)
    if ctx is None or state is None or not configured:
        raise JudgeUnavailable("chat/completions context is not configured")

    async def ajudge(judge_payload: dict[str, Any]) -> dict[str, Any]:
        prompts = None
        if callable(port(ctx, "guard_probe")):
            facts = await probe_step(
                ctx,
                GuardProbeRequest(
                    "command_prompts",
                    str(judge_payload.get("tool_name") or ""),
                    "",
                    str(judge_payload["launch_root"]),
                ),
            ).make_coro()
            prompts = facts.data
        raw_text = await arequest_auxiliary_completion_text(
            ctx=ctx,
            state=state,
            messages=_build_judge_messages(
                judge_payload, prompts=prompts, store=agent_store_of(ctx)
            ),
            max_tokens=auxiliary_llm_judge_max_tokens(ctx),
            request_label="command_moderation_judge",
            consumer="command_judge",
        )
        tags = tuple(str(tag) for tag in settings.strip_tags_projection.tags)
        return _parse_judge_json(
            raw_text,
            logger=getattr(ctx, "logger", None),
            tool_name=str(judge_payload.get("tool_name") or ""),
            tags=tags,
        )

    return ajudge


def _build_judge_messages(
    judge_payload: dict[str, Any],
    *,
    prompts: dict[str, str] | None = None,
    store: AgentStore = DEFAULT_AGENT_STORE,
) -> list[dict[str, str]]:
    system_prompt = (
        prompts["system"]
        if prompts is not None
        else load_command_judge_prompt("system", store=store)
    )
    user_template = (
        prompts["user"]
        if prompts is not None
        else load_command_judge_prompt("user", store=store)
    )
    user_prompt = user_template.format(
        judge_payload_json=json.dumps(
            judge_payload, ensure_ascii=False, indent=2
        )
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def load_command_judge_prompt(
    kind: str, *, store: AgentStore = DEFAULT_AGENT_STORE
) -> str:
    if kind == "system":
        path = (
            bundled_root() / "prompts" / "security" / "command_judge_system.md"
        )
    elif kind == "user":
        path = (
            bundled_root() / "prompts" / "security" / "command_judge_user.md"
        )
    else:
        raise ValueError("Unknown command judge prompt kind.")
    return _load_prompt_file(path, store=store, name=f"command_judge_{kind}")


def _load_prompt_file(
    path: Path,
    *,
    store: AgentStore = DEFAULT_AGENT_STORE,
    name: str = "command_judge_system",
) -> str:
    # «Недоступен» — это и «не декодируется» (`UnicodeDecodeError` подкласс
    # `ValueError`, а не `OSError`): судья отключается, а не роняет ход.
    try:
        text = store.read_text(
            AgentResource("prompt", name, str(path))
        ).strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise JudgeUnavailable(
            f"Command judge prompt file is unavailable: {path}"
        ) from exc
    if not text:
        raise JudgeUnavailable(f"Command judge prompt file is empty: {path}")
    return text


def _strip_judge_response_text(
    raw_text: str,
    *,
    tags: Sequence[str] = (),
) -> str:
    # Aux-парсер: ``<think>`` всегда, extra-теги из конфига; fence/JSON поверх.
    text = project_text_for_model(raw_text, tags=tags, strip_think=True)
    fence_match = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?```", text, flags=re.DOTALL | re.IGNORECASE
    )
    if fence_match:
        return fence_match.group(1).strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return text


def _extract_json_candidate(text: str) -> str:
    if text.startswith("{"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _verdict_regex_fallback(text: str) -> dict[str, Any] | None:
    """
    Last-resort parser: ищем явное поле verdict в ответе модели.
    Dangerous проверяем раньше safe (fail-closed при конфликте).
    """
    if _VERDICT_DANGEROUS_RE.search(text):
        return {
            "verdict": DANGEROUS_VERDICT,
            "reasons": ["Judge response parsed via regex fallback."],
            "risk_flags": ["judge_response_malformed"],
            "confidence": 0.0,
        }
    if _VERDICT_SAFE_RE.search(text):
        return {
            "verdict": SAFE_VERDICT,
            "reasons": ["Judge response parsed via regex fallback."],
            "risk_flags": [],
            "confidence": 0.0,
        }
    return None


def _log_command_judge_regex_fallback(
    logger: Any,
    *,
    tool_name: str | None,
    verdict: str,
) -> None:
    if logger is None or not hasattr(logger, "warning"):
        return
    event = {
        "event": "command_moderation_judge_regex_fallback",
        "tool_name": tool_name or "",
        "matched_verdict": verdict,
    }
    try:
        logger.warning(
            "command_moderation_judge_regex_fallback %s",
            json.dumps(event, ensure_ascii=False, sort_keys=True),
        )
    except Exception:
        return


def _parse_judge_json(
    raw_text: str,
    *,
    logger: Any = None,
    tool_name: str | None = None,
    tags: Sequence[str] = (),
) -> dict[str, Any]:
    text = _strip_judge_response_text(raw_text, tags=tags)
    if not text:
        raise JudgeUnavailable("judge returned empty content")
    json_candidate = _extract_json_candidate(text)
    try:
        return json.loads(json_candidate)
    except json.JSONDecodeError:
        fallback = _verdict_regex_fallback(text)
        if fallback is None:
            raise JudgeUnavailable("judge returned unparseable response")
        _log_command_judge_regex_fallback(
            logger,
            tool_name=tool_name,
            verdict=str(fallback["verdict"]),
        )
        return fallback


def _ask_user_confirmation(
    payload: str,
    context: ModerationContext,
    decision: ExecutionDecision,
    ctx: Any = None,
) -> bool:
    """Ожидание человека. Зовётся драйвером гварда в отдельном потоке."""
    primary_label = _primary_payload_label(context.tool_name)
    # Full payload as-is: TUI banner scrolls body + docks Y/O/N/T.
    return request_confirm(
        title="Проверка команды",
        question=f"Разрешить запуск? ({context.tool_name})",
        fields=[
            (primary_label, payload),
            ("Причина", decision.reason),
            ("Риски", ", ".join(decision.risk_flags) or "-"),
        ],
        ctx=ctx,
        decision_key=f"command:{payload}",
    )


def _primary_payload_label(tool_name: str) -> str:
    """Primary payload field label: Команда (shell) / Код (python)."""
    return "Код" if tool_name == EXEC_PYTHON_NAME else "Команда"


def _tool_blocked_result(args: tuple[Any, ...]) -> Callable[..., str] | None:
    self_obj = args[0] if args else None
    candidate = getattr(self_obj, "_command_moderation_blocked_result", None)
    return candidate if callable(candidate) else None
