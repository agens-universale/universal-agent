from __future__ import annotations

import functools
import inspect
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from gc_core.execution.ports import port
from gc_core.security.confirm_port import (
    confirmation_step,
    guard_refusal,
    refuse_in_place,
)
from gc_core.security.dual_wrapper import dual_wrap
from gc_core.security.guard_probe import GuardProbeRequest, probe_step
from gc_core.security.mode_stage import (
    ModeAction,
    remembered_denial,
    resolve_by_mode,
)
from gc_core.security.permission_mode import (
    ALIGN,
    ASK,
    CAT_EDIT,
    DENY,
    PASS,
    decision_path_key,
    floor_question_decision,
    write_decision,
    write_profile,
)
from gc_core.security.permission_mode import ALLOW as MODE_ALLOW
from gc_core.settings import (
    AppConfig,
    LoadedSettings,
    runtime_of,
    settings_of,
)
from gc_core.trace_id import trace_id_from_config

WRITE_GUARD_TITLE = "Защита записи"


@dataclass(frozen=True)
class WriteGuardConfig:
    enabled: bool = False
    mode: str = "deny"  # allow | ask | deny
    extra_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolSecurityContext:
    launch_root: str
    write_guard: WriteGuardConfig = field(default_factory=WriteGuardConfig)
    command_moderation_mode: str = "allow"
    allowed_command_examples: list[str] = field(default_factory=list)
    denied_command_examples: list[str] = field(default_factory=list)
    url_allowed_schemes: list[str] = field(
        default_factory=lambda: ["http", "https"]
    )
    url_blacklist: list[str] = field(default_factory=list)
    url_policy_enabled: bool = False
    url_policy_mode: str = "deny"
    ctx: Any = None
    state: Any = None
    logger: Any = None


class WriteGuardViolation(ValueError):
    def __init__(
        self,
        *,
        launch_root: str,
        allowed_write_roots: list[str],
        path: str,
        operation: str,
    ) -> None:
        self.error = {
            "type": "write_guard_violation",
            "launch_root": launch_root,
            "allowed_write_roots": allowed_write_roots,
            "path": path,
            "operation": operation,
            "message": "Write operation is outside allowed write_guard perimeter.",
        }
        super().__init__(self.error["message"])


def normalize_path_for_policy(
    path: str | os.PathLike[str], base_dir: str | os.PathLike[str]
) -> Path:
    raw_path = os.fspath(path)
    if not raw_path:
        raise ValueError("path must not be empty")
    if os.path.isabs(raw_path):
        absolute_path = os.path.abspath(raw_path)
    else:
        absolute_path = os.path.abspath(
            os.path.join(os.fspath(base_dir), raw_path)
        )
    return resolve_existing_parent(Path(absolute_path))


def resolve_existing_parent(path: Path) -> Path:
    path = Path(os.path.abspath(os.fspath(path)))
    if path.exists():
        return path.resolve(strict=True)

    existing_parent = path.parent
    while not existing_parent.exists():
        next_parent = existing_parent.parent
        if next_parent == existing_parent:
            raise FileNotFoundError(
                f"No existing parent found for path: {path}"
            )
        existing_parent = next_parent

    resolved_parent = existing_parent.resolve(strict=True)
    return resolved_parent / path.relative_to(existing_parent)


def is_within_root(
    candidate: Path | str,
    root: Path | str,
    *,
    case_insensitive: bool = False,
) -> bool:
    candidate_str = os.fspath(candidate)
    root_str = os.fspath(root)
    if case_insensitive:
        candidate_str = candidate_str.casefold()
        root_str = root_str.casefold()
    try:
        return os.path.commonpath([root_str, candidate_str]) == root_str
    except ValueError:
        return False


def is_within_any_root(
    candidate: Path | str,
    roots: Iterable[Path | str],
    *,
    case_insensitive: bool = False,
) -> bool:
    return any(
        is_within_root(candidate, root, case_insensitive=case_insensitive)
        for root in roots
    )


def check_write_path_allowed(
    candidate_path: str,
    launch_root: str,
    extra_write_roots: list[str] | None = None,
    base_dir: str | None = None,
    *,
    operation: str = "write",
    case_insensitive: bool | None = None,
) -> Path:
    effective_base_dir = base_dir or launch_root
    target = normalize_path_for_policy(candidate_path, effective_base_dir)
    root = normalize_path_for_policy(launch_root, effective_base_dir)
    extra_roots = [
        normalize_path_for_policy(path, effective_base_dir)
        for path in (extra_write_roots or [])
    ]
    allowed_roots = [root, *extra_roots]
    if case_insensitive is None:
        case_insensitive = os.name == "nt"
    if is_within_any_root(
        target, allowed_roots, case_insensitive=case_insensitive
    ):
        return target
    raise WriteGuardViolation(
        launch_root=str(root),
        allowed_write_roots=[str(path) for path in allowed_roots],
        path=str(target),
        operation=operation,
    )


def resolve_tool_context(*args: Any, **kwargs: Any) -> ToolSecurityContext:
    self_obj = args[0] if args else None
    ctx = kwargs.get("ctx") or getattr(self_obj, "_ctx", None)
    state = kwargs.get("state") or getattr(self_obj, "_state", None)
    logger = kwargs.get("logger") or getattr(ctx, "logger", None)
    source = _settings_source(self_obj, ctx, kwargs)
    settings = settings_of(source)
    runtime = runtime_of(source)
    security = settings.security
    project_data = getattr(state, "initial_project_data", None)
    launch_root = (
        runtime.launch_root
        or runtime.project_root
        or (project_data or {}).get("projectDir")
        or os.getcwd()
    )
    return ToolSecurityContext(
        launch_root=str(launch_root),
        write_guard=WriteGuardConfig(
            enabled=bool(security.write_guard.enabled),
            mode=str(security.write_guard.mode).strip().lower(),
            extra_paths=list(security.write_guard.extra_paths or []),
        ),
        command_moderation_mode=str(security.command_moderation.mode)
        .strip()
        .lower(),
        allowed_command_examples=list(
            security.command_moderation.allowed_command_examples or []
        ),
        denied_command_examples=list(
            security.command_moderation.denied_command_examples or []
        ),
        url_allowed_schemes=list(
            security.url_policy.allowed_schemes or ["http", "https"]
        ),
        url_blacklist=list(security.url_policy.url_blacklist or []),
        url_policy_enabled=bool(security.url_policy.enabled),
        url_policy_mode=str(security.url_policy.mode).strip().lower(),
        ctx=ctx,
        state=state,
        logger=logger,
    )


def _protected_write_block(
    candidate_path, context, func, args, kwargs, blocked_result_factory
):
    """Floor carve-out (Spec B §6): ``(blocked, confirmed, flags)``.

    ``blocked`` — готовый отказ записи в защищённый путь либо None (можно
    писать). ``confirmed`` — человек ответил «да» на вопрос пола про ЭТОТ
    вызов: вопрос описывает запись целиком (путь), поэтому ниже её не
    переспрашивают; сильнее остаётся только явный ``write_guard.mode: deny``.
    ``flags`` — пометки для судьи действий: там, где на вопрос пола отвечает
    он, пол сам никого не спрашивает и передаёт пометку стадии режима — на
    запись приходится одна проверка, а не две.

    Генератор, как и тело гарда: подтверждение человека идёт запросом
    `yield OffLoop(...)` драйверу, поэтому звать — через `yield from`.
    """
    from gc_core.security.confirm_port import request_confirm
    from gc_core.security.media_roots import media_roots
    from gc_core.security.protected_paths import (
        PROTECTED_PATH_TITLE,
        protected_decision,
        protection_enabled,
    )

    if not protection_enabled(_settings_source(None, context.ctx)):
        return None, False, ()
    if callable(port(context.ctx, "guard_probe")):
        facts = yield probe_step(
            context.ctx,
            GuardProbeRequest(
                "protected_write",
                _tool_name(func, args),
                candidate_path,
                context.launch_root,
            ),
        )
        _decision = facts.protected_decision
    else:
        _decision = protected_decision(
            candidate_path,
            cwd=context.launch_root,
            media=media_roots(context.ctx),
        )
    if _decision == "deny":
        _err = {
            "type": "protected_path_denied",
            "path": candidate_path,
            "reason": "Path is protected (operational .gc / config).",
        }
        audit_policy_decision(
            context,
            tool_name=_tool_name(func, args),
            decision="blocked",
            reason_code="protected_deny",
        )
        return (
            _blocked_result(
                func,
                _err,
                args,
                kwargs,
                blocked_result_factory=blocked_result_factory,
            ),
            False,
            (),
        )
    floor_answer = (
        floor_question_decision(
            (
                write_profile(context.ctx).inside,
                write_profile(context.ctx).outside,
            )
        )
        if _decision == "ask"
        else None
    )
    if floor_answer == ALIGN:
        from gc_core.security.action_judge import FLAG_PROTECTED_PATH

        return None, False, (FLAG_PROTECTED_PATH,)
    if floor_answer == PASS:
        return None, False, ()  # режим запретит запись сам, без вопроса
    if _decision == "ask":
        # Ожидание человека уходит в поток: в TUI это до 600 с, и
        # на event loop вместе с ним встали бы субагенты и
        # keep-alive MCP-сессий. Исполняет драйвер `dual_wrap`.
        _ok = yield confirmation_step(
            local_request=request_confirm,
            title=PROTECTED_PATH_TITLE,
            question=f"Разрешить запись в защищённый путь {candidate_path}?",
            fields=[("path", candidate_path)],
            ctx=context.ctx,
            decision_key=_write_key(candidate_path, context),
        )
        if not _ok:
            _err = guard_refusal(
                context.ctx, "protected_path_denied", path=candidate_path
            )
            audit_policy_decision(
                context,
                tool_name=_tool_name(func, args),
                decision="blocked",
                reason_code="protected_ask_cancelled",
                error_type=_err["type"],
            )
            return (
                _blocked_result(
                    func,
                    _err,
                    args,
                    kwargs,
                    blocked_result_factory=blocked_result_factory,
                ),
                False,
                (),
            )
        # confirmed → fall through. `confirmed`, а не `executed`: запись ещё не
        # выполнена — явный `mode: deny` ниже сильнее; итог пишет следующая
        # строка аудита.
        audit_policy_decision(
            context,
            tool_name=_tool_name(func, args),
            decision="confirmed",
            reason_code="protected_ask_confirmed",
            risk_flags=["touches_protected_paths"],
        )
        return None, True, ()
    return None, False, ()


def write_decision_key(candidate_path, context) -> str:
    """Ключ решения про запись в путь — один на вопрос пола, вопрос режима и
    вопрос нарушения периметра: ответ на любой закрывает остальные."""
    return decision_path_key("write", candidate_path, context.launch_root)


_write_key = write_decision_key


def write_mode_stage(
    candidate_path,
    context,
    tool_name,
    operation,
    *,
    floor_confirmed=False,
    floor_flags=(),
):
    """Стадия режима разрешений для записи: ``(call_now, error)``.

    ``error`` — тело отказа; ``call_now`` — запись разрешена здесь и сейчас,
    дальнейшие проверки периметра не нужны; оба пусты — режим ничего не
    добавил, решает обычный ход защиты.

    Стоит после пола защищённых путей и ДО ``write_guard.enabled``: выключенная
    в конфиге защита режим не отменяет. Общая для декоратора и для тула,
    который зовёт защиту записи сам (``download_file``).
    """
    key = write_decision_key(candidate_path, context)
    denied = remembered_denial(context, tool_name, key, path=candidate_path)
    if denied is not None:
        return False, denied
    if write_profile(context.ctx).adds_nothing:
        return False, None
    try:
        yield from check_write_guard_path(
            candidate_path, context, tool_name, operation
        )
        inside = True
    except WriteGuardViolation:
        inside = False
    except (ValueError, OSError):
        # Путь не поддаётся классификации (пустой, неразрешимый префикс) —
        # дело валидации самого тула: он вернёт свой конверт. Но если пол
        # уже отдал вопрос судье, уйти отсюда без проверки нельзя.
        if not floor_flags:
            return False, None
        inside = True
    decision = write_decision(context.ctx, inside=inside)
    if floor_flags and decision != DENY:
        # Пол пометил путь (файл инструкций): решает судья, где бы путь ни
        # лежал и что бы ни разрешал явный ключ периметра.
        decision = ALIGN
    if decision in (MODE_ALLOW, PASS):
        return False, None
    if decision == ASK and floor_confirmed:
        # Пол уже спросил человека про этот путь — второй вопрос не нужен.
        audit_policy_decision(
            context,
            tool_name=tool_name,
            decision="executed",
            reason_code="protected_ask_confirmed",
        )
        return True, None
    from gc_core.security.action_judge import FLAG_OUTSIDE_PROJECT

    allowed, error = yield from resolve_by_mode(
        decision,
        ModeAction(
            context=context,
            tool_name=tool_name,
            category=CAT_EDIT,
            decision_key=key,
            question=(
                "Запись в файл",
                f"Разрешить запись в {candidate_path}?",
                [("Путь", candidate_path), ("Операция", operation)],
            ),
            flags=(
                *floor_flags,
                *(() if inside else (FLAG_OUTSIDE_PROJECT,)),
            ),
            error_fields={"path": candidate_path},
            deny_type="write_denied_by_mode",
            deny_reason="Write not permitted in the current permission mode.",
        ),
    )
    return (True, None) if allowed else (False, error)


def _resolve_write_violation(
    exc,
    context,
    candidate_path,
    operation,
    func,
    args,
    kwargs,
    blocked_result_factory,
    floor_confirmed=False,
):
    """Нарушение периметра по режиму: (True, None) — писать можно; (False, blocked) — отказ.

    Генератор: подтверждение в режиме ask — запрос `yield OffLoop(...)`
    драйверу, звать через `yield from`.
    """
    tool_name = _tool_name(func, args)
    wg_mode = (context.write_guard.mode or "deny").strip().lower()
    if wg_mode == "allow":
        # Документированный пропуск: помечаем в audit, но даём
        # write пройти. Используется для baseline-тестов.
        audit_policy_decision(
            context,
            tool_name=tool_name,
            decision="executed",
            reason_code="policy_allow_out_of_perimeter",
            risk_flags=["out_of_perimeter"],
        )
        return True, None
    if wg_mode == "ask" and floor_confirmed:
        # Человек уже разрешил этот вызов на вопросе пола — второй вопрос про
        # тот же путь не задаётся.
        audit_policy_decision(
            context,
            tool_name=tool_name,
            decision="executed",
            reason_code="policy_ask_confirmed",
            risk_flags=["out_of_perimeter"],
        )
        return True, None
    if wg_mode == "ask":
        # Тот же приём, что и у защищённых путей выше: подтверждение
        # ждётся в потоке, а не на event loop вызывающего.
        confirmed = yield confirmation_step(
            title=WRITE_GUARD_TITLE,
            question=f"Разрешить запись вне периметра проекта в {candidate_path}?",
            fields=[
                ("Tool", tool_name),
                ("Operation", operation),
                ("Path", candidate_path),
                ("Allowed roots", exc.error.get("allowed_write_roots") or []),
            ],
            ctx=context.ctx,
            decision_key=_write_key(candidate_path, context),
            local_wait=functools.partial(
                _ask_user_confirmation_for_write,
                tool_name=tool_name,
                path=candidate_path,
                operation=operation,
                error=exc.error,
                ctx=context.ctx,
                decision_key=_write_key(candidate_path, context),
            ),
        )
        if confirmed:
            audit_policy_decision(
                context,
                tool_name=tool_name,
                decision="executed",
                reason_code="policy_ask_confirmed",
                risk_flags=["out_of_perimeter"],
            )
            return True, None
        # cancelled — продолжаем в стандартный blocked-flow ниже,
        # но помечаем error.type для отличимости в логах.
        refuse_in_place(
            exc.error,
            context.ctx,
            "write_guard_cancelled_by_user",
            path=candidate_path,
        )
    audit_policy_decision(
        context,
        tool_name=tool_name,
        decision="blocked",
        reason_code="policy_deny"
        if wg_mode == "deny"
        else "policy_ask_cancelled",
        error_type=exc.error.get("type"),
    )
    return False, _blocked_result(
        func,
        exc.error,
        args,
        kwargs,
        blocked_result_factory=blocked_result_factory,
    )


def check_write_guard_path(candidate_path, context, tool_name, operation):
    """Apply the same write perimeter check to decorators and internal writes."""
    if callable(port(context.ctx, "guard_probe")):
        facts = yield probe_step(
            context.ctx,
            GuardProbeRequest(
                "write_path",
                tool_name,
                candidate_path,
                context.launch_root,
                operation,
            ),
        )
        if facts.violation is not None:
            error = facts.violation
            raise WriteGuardViolation(
                launch_root=error["launch_root"],
                allowed_write_roots=error["allowed_write_roots"],
                path=error["path"],
                operation=error["operation"],
            )
    else:
        check_write_path_allowed(
            candidate_path,
            context.launch_root,
            context.write_guard.extra_paths,
            base_dir=context.launch_root,
            operation=operation,
        )


def _write_guard_flow(
    func,
    args,
    kwargs,
    path_arg,
    path_extractor,
    operation,
    blocked_result_factory,
):
    """Тело декоратора — генератор: `(yield)` — точка вызова самого тула."""
    context = resolve_tool_context(*args, **kwargs)
    # Floor carve-out: runs BEFORE the write_guard.enabled gate so it
    # applies unconditionally (Spec B §6).
    _, candidate_path = _guard_path_argument(
        func, args, kwargs, path_arg, path_extractor
    )
    floor_confirmed = False
    floor_flags: tuple = ()
    if candidate_path is not None:
        (
            blocked,
            floor_confirmed,
            floor_flags,
        ) = yield from _protected_write_block(
            candidate_path,
            context,
            func,
            args,
            kwargs,
            blocked_result_factory,
        )
        if blocked is not None:
            return blocked
        call_now, error = yield from write_mode_stage(
            candidate_path,
            context,
            _tool_name(func, args),
            operation,
            floor_confirmed=floor_confirmed,
            floor_flags=floor_flags,
        )
        if error is not None:
            return _blocked_result(
                func,
                error,
                args,
                kwargs,
                blocked_result_factory=blocked_result_factory,
            )
        if call_now:
            return (yield)
    if not context.write_guard.enabled:
        audit_policy_decision(
            context,
            tool_name=_tool_name(func, args),
            decision="executed",
            reason_code="policy_disabled",
        )
        return (yield)
    if candidate_path is None:
        extracted, candidate_path = _guard_path_argument(
            func, args, kwargs, path_arg, path_extractor
        )
        if not extracted:
            return (yield)
    try:
        yield from check_write_guard_path(
            candidate_path, context, _tool_name(func, args), operation
        )
    except WriteGuardViolation as exc:
        allowed, blocked = yield from _resolve_write_violation(
            exc,
            context,
            candidate_path,
            operation,
            func,
            args,
            kwargs,
            blocked_result_factory,
            floor_confirmed=floor_confirmed,
        )
        if allowed:
            return (yield)
        return blocked
    audit_policy_decision(
        context,
        tool_name=_tool_name(func, args),
        decision="executed",
        reason_code="policy_allow",
    )
    return (yield)


def enforce_write_guard(
    path_arg: str | None = None,
    path_extractor: Callable[..., str] | None = None,
    operation: str = "write",
    blocked_result_factory: Callable[[dict[str, Any]], str] | None = None,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    def decorator(func: Callable[..., str]) -> Callable[..., str]:
        def _flow(*args: Any, **kwargs: Any):
            return (
                yield from _write_guard_flow(
                    func,
                    args,
                    kwargs,
                    path_arg,
                    path_extractor,
                    operation,
                    blocked_result_factory,
                )
            )

        return dual_wrap(func, _flow)

    return decorator


def audit_policy_decision(
    context: ToolSecurityContext,
    *,
    tool_name: str,
    decision: str,
    reason_code: str,
    risk_flags: list[str] | None = None,
    error_type: str | None = None,
    reason: str | None = None,
    call_id: str | None = None,
) -> None:
    logger = context.logger
    if logger is None or not hasattr(logger, "info"):
        return
    event: dict[str, Any] = {
        "event": "security_policy_decision",
        "tool_name": tool_name,
        "decision": decision,
        "reason_code": reason_code,
        "risk_flags": risk_flags or [],
    }
    if error_type:
        event["error_type"] = error_type
    # Слова судьи действий и вызов, к которому они относятся: по ним
    # разбирают ложные отказы и пропуски на реальной работе.
    if reason:
        event["reason"] = reason
    if call_id:
        event["call_id"] = call_id
    # Внутрь payload, а не в extra=: событие сериализуется в текст сообщения,
    # поэтому только так аудит-объект остаётся самодостаточным. В jsonl фильтр
    # логгера дополнительно ставит trace_id верхним полем записи.
    trace_id = trace_id_from_config(
        _settings_source(None, getattr(context, "ctx", None))
    )
    if trace_id:
        event["trace_id"] = trace_id
    try:
        logger.info(
            "security_policy_decision %s",
            json.dumps(event, ensure_ascii=False, sort_keys=True),
        )
    except Exception:
        return


def _settings_source(
    self_obj: Any, ctx: Any, kwargs: Mapping[str, Any] | None = None
) -> Any:
    """Откуда гарду читать настройки (то, что принимает ``settings_of``).

    Контекст рана с типизированными ``settings``, иначе явные ``settings=``
    тула (``self._settings``), иначе контекст тула (``self._ctx``). Ничего
    нет — ``None``: ``settings_of(None)`` даёт умолчания модели, а они
    secure-by-default (гарды включены). Аргументы вызова (``kwargs``) — это
    аргументы модели, источником настроек они не бывают; параметр оставлен
    ради прежней сигнатуры.
    """
    del kwargs
    if _has_settings(ctx):
        return ctx
    explicit = getattr(self_obj, "_settings", None)
    if explicit is not None:
        return explicit
    tool_ctx = getattr(self_obj, "_ctx", None)
    if _has_settings(tool_ctx):
        return tool_ctx
    return None


def _has_settings(ctx: Any) -> bool:
    """Несёт ли ctx настройки рана."""
    return isinstance(
        getattr(ctx, "settings", None), (AppConfig, LoadedSettings)
    )


def _guard_path_argument(func, args, kwargs, path_arg, path_extractor):
    """Keep extractor values intact; missing paths defer to tool validation."""
    try:
        return True, _extract_named_value(
            func, args, kwargs, path_arg, path_extractor, "path"
        )
    except ValueError:
        return False, None


def _extract_named_value(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    arg_name: str | None,
    extractor: Callable[..., str] | None,
    default_arg_name: str,
) -> str:
    if extractor is not None:
        return extractor(*args, **kwargs)
    effective_arg_name = arg_name or default_arg_name
    bound = inspect.signature(func).bind_partial(*args, **kwargs)
    value = bound.arguments.get(effective_arg_name)
    if not isinstance(value, str):
        raise ValueError(f"{effective_arg_name} must be a string")
    return value


def _tool_name(func: Callable[..., Any], args: tuple[Any, ...]) -> str:
    self_obj = args[0] if args else None
    description = getattr(self_obj, "tool_description", None)
    if callable(description):
        try:
            spec = description()
            if isinstance(spec, dict) and isinstance(spec.get("name"), str):
                return spec["name"]
        except Exception:
            pass
    return getattr(func, "__name__", "unknown")


def prompt_user_confirmation_choice(
    *,
    title: str,
    fields: list[tuple[str, Any]],
    question: str,
    decision_key: str | None = None,
    permission_mode: str = "edit",
) -> str:
    """Non-TUI fallback confirm (2×2), same key matrix as TUI banner.

    Product UX for interactive ask is TUI; headless CLI is expected to use
    policy allow|deny without interactive ask. This path remains for tty
    scripts / legacy callers that still hit request_confirm with is_tui=False.

    Auto-deny_once if stdin is not a tty (CI/background).
    Keys: Latin/Cyrillic same-key sets from confirm_port (y/н, o/щ, n/т, t/е).
    EOF (Ctrl+D) → deny_remember; Ctrl+C → deny_once (interrupt ≠ policy seed).
    """
    # Nested import: path_policy ↔ confirm_port cycle if top-level.
    from gc_core.security.confirm_port import (
        CONFIRM_DENY_ONCE,
        confirm_choice_hint,
        normalize_confirm_key,
    )

    if not sys.stdin or not sys.stdin.isatty():
        return CONFIRM_DENY_ONCE
    # Payload-first: primary labels (Команда/Код/…) before meta (SPEC §3.5b).
    _primary = frozenset(
        {
            "команда",
            "код",
            "command",
            "code",
            "path",
            "url",
            "путь",
        }
    )

    def _is_primary(label: object) -> bool:
        return str(label).strip().casefold() in _primary

    # Stable order: keep caller order, but emit primary block first.
    ordered = sorted(fields, key=lambda item: 0 if _is_primary(item[0]) else 1)
    lines = [f"\n{title}: confirmation required.", ""]
    for label, value in ordered:
        if _is_primary(label):
            lines.append(f"{label}:\n{value}")
            lines.append("")
        else:
            lines.append(f"{label}: {value}")
    lines.append(question)
    # Same mnemonic as TUI; Esc/timeout only exist in TUI runner — omit here.
    lines.append(f"{confirm_choice_hint(decision_key, permission_mode)}: ")
    try:
        _drop_typed_ahead()
        print("\n".join(lines), file=sys.stderr, end="")
        sys.stderr.flush()
        answer = _read_confirmation_line()
    except EOFError:
        # Конец ввода — не выбор запрета на сессию: запрет разовый.
        return CONFIRM_DENY_ONCE
    except KeyboardInterrupt:
        return CONFIRM_DENY_ONCE
    if answer is None:
        return CONFIRM_DENY_ONCE
    return normalize_confirm_key(answer)


#: Шаг опроса stdin в ответе на `y/n`. Идёт в рабочем потоке гарда, loop не
#: держит; 50 мс человеку незаметны.
_CONFIRM_POLL_SEC = 0.05


def _polling_console() -> Any:
    """Консоль Windows для опроса нажатий — модуль `msvcrt`; вне Windows ``None``."""
    if os.name != "nt":
        return None
    import msvcrt

    return msvcrt


def _drop_typed_ahead() -> None:
    """Выбросить набранное ДО вопроса: `y/n` отвечает только ввод после него.

    Иначе разрешение выдавало бы то, что человек напечатал, пока агент работал,
    или строка, начатая для вопроса агента, который потом сняли по Ctrl+C.
    """
    from gc_core.utils.stdin_lines import discard_unread, pollable_stdin_fd

    fd = pollable_stdin_fd()
    if fd is not None:
        discard_unread(fd)
        return
    console = _polling_console()
    if console is not None:
        while console.kbhit():
            console.getwch()


def _read_confirmation_line() -> str | None:
    """Строка ответа на `y/n`; ``None`` — вопрос бросили, отвечать больше некому.

    Читаем тем же читателем, что и вопросы агента (`gc_core.utils.stdin_lines`),
    а не `input()`. Подтверждение ждёт в рабочем потоке гарда, и после Ctrl+C
    его бросают: `input()` остался бы в `read()` и забрал строку, набранную уже
    для следующего вопроса. Опрос же видит, что вызов брошен, и уходит, не
    трогая чужую строку.

    На Windows `select` к консоли неприменим — там опрос нажатий `msvcrt`, с тем
    же уходом брошенного вызова. Прежнее `input()` осталось только для stdin без
    `fileno()` вне Windows (подменённого): брошенный вызов строку там унести
    ещё может, но решения по ней не запишет — это отсекает `request_confirm`.
    """
    # Lazy: security → tools/utils на module load ломает import graph.
    from gc_core.tools.connect_mcp.abandonable import current_call_abandoned
    from gc_core.utils.stdin_lines import pollable_stdin_fd, take_line

    fd = pollable_stdin_fd()
    if fd is None:
        console = _polling_console()
        if console is not None:
            return _read_console_line(console, give_up=current_call_abandoned)
        return input().strip()
    while True:
        if current_call_abandoned():
            return None
        line = take_line(fd, give_up=current_call_abandoned)
        if line is not None:
            return line.strip()
        time.sleep(_CONFIRM_POLL_SEC)


def _read_console_line(
    console: Any, *, give_up: Callable[[], bool]
) -> str | None:
    """Строка с консоли Windows опросом нажатий; ``None`` — вопрос бросили.

    `kbhit` не блокирует, поэтому брошенный вызов замечает метку и уходит, не
    взяв ни одного нажатия, — в отличие от `input()`, который остался бы в
    чтении. `getwch` набранное не показывает, эхо печатаем сами. Backspace
    стирает; стрелки и F-клавиши приходят двумя кодами (префикс и код клавиши)
    и пропускаются целиком. Ctrl+Z в начале строки — конец ввода, как у
    `input()` на Windows.
    """
    chars: list[str] = []
    while True:
        if give_up():
            return None
        if not console.kbhit():
            time.sleep(_CONFIRM_POLL_SEC)
            continue
        line = _apply_console_key(console, console.getwch(), chars)
        if line is not None:
            return line


def _apply_console_key(console: Any, key: str, chars: list[str]) -> str | None:
    """Одно нажатие для `_read_console_line`: готовая строка по Enter, иначе ``None``.

    ``chars`` — набранное до сих пор, правится на месте.
    """
    if key in ("\x00", "\xe0"):
        console.getwch()
    elif key in ("\r", "\n"):
        console.putwch("\r")
        console.putwch("\n")
        return "".join(chars).strip()
    elif key == "\x1a" and not chars:
        raise EOFError
    elif key == "\x08":
        if chars:
            chars.pop()
            for ch in "\x08 \x08":
                console.putwch(ch)
    elif key.isprintable():
        chars.append(key)
        console.putwch(key)
    return None


def _ask_user_confirmation_for_write(
    *,
    tool_name: str,
    path: str,
    operation: str,
    error: dict[str, Any],
    ctx: Any = None,
    decision_key: str | None = None,
) -> bool:
    from gc_core.security.confirm_port import request_confirm

    return request_confirm(
        title=WRITE_GUARD_TITLE,
        question=f"Разрешить запись вне периметра проекта в {path}?",
        fields=[
            ("Tool", tool_name),
            ("Operation", operation),
            ("Path", path),
            ("Allowed roots", error.get("allowed_write_roots") or []),
        ],
        ctx=ctx,
        decision_key=decision_key,
    )


def _blocked_result(
    func: Callable[..., Any],
    error: dict[str, Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    blocked_result_factory: Callable[[dict[str, Any]], str] | None,
) -> str:
    if blocked_result_factory is not None:
        return blocked_result_factory(error)
    self_obj = args[0] if args else None
    tool_blocked_result = getattr(
        self_obj, "_write_guard_blocked_result", None
    )
    if callable(tool_blocked_result):
        return tool_blocked_result(error, *args[1:], **kwargs)
    return json.dumps(
        {
            "tool": getattr(func, "__name__", "unknown"),
            "ok": False,
            "error": error,
        },
        ensure_ascii=False,
    )
