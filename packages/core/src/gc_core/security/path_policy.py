from __future__ import annotations

import functools
import inspect
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any, Callable, Iterable

from gc_core.trace_id import trace_id_from_config


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
    url_allowed_schemes: list[str] = field(default_factory=lambda: ["http", "https"])
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


def normalize_path_for_policy(path: str | os.PathLike[str], base_dir: str | os.PathLike[str]) -> Path:
    raw_path = os.fspath(path)
    if not raw_path:
        raise ValueError("path must not be empty")
    if os.path.isabs(raw_path):
        absolute_path = os.path.abspath(raw_path)
    else:
        absolute_path = os.path.abspath(os.path.join(os.fspath(base_dir), raw_path))
    return resolve_existing_parent(Path(absolute_path))


def resolve_existing_parent(path: Path) -> Path:
    path = Path(os.path.abspath(os.fspath(path)))
    if path.exists():
        return path.resolve(strict=True)

    existing_parent = path.parent
    while not existing_parent.exists():
        next_parent = existing_parent.parent
        if next_parent == existing_parent:
            raise FileNotFoundError(f"No existing parent found for path: {path}")
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
    if is_within_any_root(target, allowed_roots, case_insensitive=case_insensitive):
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
    config_dict = _extract_config_dict(self_obj, ctx, kwargs)
    project_data = getattr(state, "initial_project_data", None)
    launch_root = (
        config_dict.get("launch_root")
        or config_dict.get("project_root")
        or (project_data or {}).get("projectDir")
        or os.getcwd()
    )
    # Runtime-fallback при отсутствии блока — OFF. На нормальном пути load_config
    # кладёт write_guard/url_policy/command_moderation из pydantic (secure-by-default:
    # enabled=True, mode=ask), поэтому эти fallback'и достижимы лишь в обход
    # load_config (bespoke/тест-конфиги), а не в проде.
    moderation = config_dict.get("command_moderation") or {}
    write_guard_cfg = config_dict.get("write_guard") or {}
    url_policy_cfg = config_dict.get("url_policy") or {}
    return ToolSecurityContext(
        launch_root=str(launch_root),
        write_guard=WriteGuardConfig(
            enabled=bool(write_guard_cfg.get("enabled", False)),
            mode=str(write_guard_cfg.get("mode", "deny")).strip().lower(),
            extra_paths=list(write_guard_cfg.get("extra_paths") or []),
        ),
        command_moderation_mode=str(moderation.get("mode", "allow")).strip().lower(),
        allowed_command_examples=list(moderation.get("allowed_command_examples") or []),
        denied_command_examples=list(moderation.get("denied_command_examples") or []),
        url_allowed_schemes=list(url_policy_cfg.get("allowed_schemes") or ["http", "https"]),
        url_blacklist=list(url_policy_cfg.get("url_blacklist") or []),
        url_policy_enabled=bool(url_policy_cfg.get("enabled", False)),
        url_policy_mode=str(url_policy_cfg.get("mode", "deny")).strip().lower(),
        ctx=ctx,
        state=state,
        logger=logger,
    )


def enforce_write_guard(
    path_arg: str | None = None,
    path_extractor: Callable[..., str] | None = None,
    operation: str = "write",
    blocked_result_factory: Callable[[dict[str, Any]], str] | None = None,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    def decorator(func: Callable[..., str]) -> Callable[..., str]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            context = resolve_tool_context(*args, **kwargs)
            # Floor carve-out: runs BEFORE the write_guard.enabled gate so it
            # applies unconditionally (Spec B §6).
            try:
                candidate_path = _extract_named_value(
                    func,
                    args,
                    kwargs,
                    path_arg,
                    path_extractor,
                    "path",
                )
            except ValueError:
                candidate_path = None
            if candidate_path is not None:
                from gc_core.security.protected_paths import protected_decision, protection_enabled
                from gc_core.security.confirm_port import request_confirm
                _cfg = getattr(context.ctx, "config_dict", None) or {}
                if protection_enabled(_cfg):
                    _decision = protected_decision(candidate_path, cwd=context.launch_root)
                    if _decision == "deny":
                        _err = {
                            "type": "protected_path_denied",
                            "path": candidate_path,
                            "message": "Path is protected (operational .gc / config).",
                        }
                        audit_policy_decision(
                            context,
                            tool_name=_tool_name(func, args),
                            decision="blocked",
                            reason_code="protected_deny",
                        )
                        return _blocked_result(
                            func, _err, args, kwargs,
                            blocked_result_factory=blocked_result_factory,
                        )
                    if _decision == "ask":
                        _ok = request_confirm(
                            title="Защита записи",
                            question=f"Разрешить запись в {candidate_path}?",
                            fields=[("path", candidate_path)],
                            ctx=context.ctx,
                            decision_key=f"write:{candidate_path}",
                        )
                        if not _ok:
                            _err = {
                                "type": "protected_path_denied",
                                "path": candidate_path,
                                "message": "Write to instruction file not confirmed.",
                            }
                            return _blocked_result(
                                func, _err, args, kwargs,
                                blocked_result_factory=blocked_result_factory,
                            )
                        # confirmed → fall through
            if not context.write_guard.enabled:
                audit_policy_decision(
                    context,
                    tool_name=_tool_name(func, args),
                    decision="executed",
                    reason_code="policy_disabled",
                )
                return func(*args, **kwargs)
            if candidate_path is None:
                try:
                    candidate_path = _extract_named_value(
                        func,
                        args,
                        kwargs,
                        path_arg,
                        path_extractor,
                        "path",
                    )
                except ValueError:
                    return func(*args, **kwargs)
            try:
                check_write_path_allowed(
                    candidate_path,
                    context.launch_root,
                    context.write_guard.extra_paths,
                    base_dir=context.launch_root,
                    operation=operation,
                )
            except WriteGuardViolation as exc:
                wg_mode = (context.write_guard.mode or "deny").strip().lower()
                if wg_mode == "allow":
                    # Документированный пропуск: помечаем в audit, но даём
                    # write пройти. Используется для baseline-тестов.
                    audit_policy_decision(
                        context,
                        tool_name=_tool_name(func, args),
                        decision="executed",
                        reason_code="policy_allow_out_of_perimeter",
                        risk_flags=["out_of_perimeter"],
                    )
                    return func(*args, **kwargs)
                if wg_mode == "ask":
                    confirmed = _ask_user_confirmation_for_write(
                        tool_name=_tool_name(func, args),
                        path=candidate_path,
                        operation=operation,
                        error=exc.error,
                        ctx=context.ctx,
                        decision_key=f"write:{candidate_path}",
                    )
                    if confirmed:
                        audit_policy_decision(
                            context,
                            tool_name=_tool_name(func, args),
                            decision="executed",
                            reason_code="policy_ask_confirmed",
                            risk_flags=["out_of_perimeter"],
                        )
                        return func(*args, **kwargs)
                    # cancelled — продолжаем в стандартный blocked-flow ниже,
                    # но помечаем error.type для отличимости в логах.
                    exc.error["type"] = "write_guard_cancelled_by_user"
                    exc.error["message"] = (
                        f"User declined the write to '{candidate_path}'. Do NOT retry the same "
                        "write, and do NOT silently write somewhere else — ask the user how to "
                        "proceed, or stop and report that it was declined."
                    )
                audit_policy_decision(
                    context,
                    tool_name=_tool_name(func, args),
                    decision="blocked",
                    reason_code="policy_deny" if wg_mode == "deny" else "policy_ask_cancelled",
                    error_type=exc.error.get("type"),
                )
                return _blocked_result(
                    func,
                    exc.error,
                    args,
                    kwargs,
                    blocked_result_factory=blocked_result_factory,
                )
            audit_policy_decision(
                context,
                tool_name=_tool_name(func, args),
                decision="executed",
                reason_code="policy_allow",
            )
            return func(*args, **kwargs)

        return wrapper

    return decorator


def audit_policy_decision(
    context: ToolSecurityContext,
    *,
    tool_name: str,
    decision: str,
    reason_code: str,
    risk_flags: list[str] | None = None,
    error_type: str | None = None,
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
    # Внутрь payload, а не в extra=: событие сериализуется в текст сообщения,
    # поэтому только так аудит-объект остаётся самодостаточным. В jsonl фильтр
    # логгера дополнительно ставит trace_id верхним полем записи.
    trace_id = trace_id_from_config(
        getattr(getattr(context, "ctx", None), "config_dict", None)
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


def _extract_config_dict(self_obj: Any, ctx: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    explicit = kwargs.get("config_dict")
    if isinstance(explicit, dict):
        return explicit
    ctx_config = getattr(ctx, "config_dict", None)
    if isinstance(ctx_config, dict):
        return ctx_config
    self_config = getattr(self_obj, "_config_dict", None)
    if isinstance(self_config, dict):
        return self_config
    return {}


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
) -> str:
    """CLI-подтверждение с 3 вариантами: y=once, s=session, n/прочее=deny.
    Auto-deny если stdin не tty (фоновый/CI запуск)."""
    if not sys.stdin or not sys.stdin.isatty():
        return "deny"
    lines = [f"\n{title}: confirmation required."]
    for label, value in fields:
        lines.append(f"{label}: {value}")
    lines.append(f"{question} [y=раз / s=сессия / N=нет]: ")
    try:
        print("\n".join(lines), file=sys.stderr, end="")
        sys.stderr.flush()
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "deny"
    if answer in {"y", "yes"}:
        return "once"
    if answer in {"s", "session"}:
        return "session"
    return "deny"



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
        title="Защита записи",
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
    tool_blocked_result = getattr(self_obj, "_write_guard_blocked_result", None)
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
