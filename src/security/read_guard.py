"""
Read guard — path-policy для read-инструментов агента.

См. read-guard-spec.md. В v1 реализован только Контур 1 (детерминированный
path-policy). Контур 2 (sensitivity scan) и Контур 3 (anti-exfiltration)
оставлены на следующие итерации.

Декоратор `enforce_read_guard` применяется к read-тулам аналогично тому,
как `enforce_write_guard` применяется к write-тулам.
"""
from __future__ import annotations

import fnmatch
import functools
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from security.path_policy import (
    audit_policy_decision,
    resolve_tool_context,
    _extract_named_value,
    _tool_name,
)


_GLOB_WILDCARD_MARKERS = {"*", "all"}


def _resolve_skills_root(config_dict: dict[str, Any]) -> str | None:
    """
    Возвращает абсолютный путь до каталога скиллов (где лежат SKILL.md),
    чтобы read_guard мог автоматически разрешать их чтение без явной записи
    в `allowed_read_paths`.

    Логика:
      - если в config.yml задан `skills_data_path` — используется он
        (single-source mode);
      - иначе — system root из `tools.skills.registry._system_skills_root`
        (`<project_root>/skills`).

    Импорт ленивый, чтобы избежать циклической зависимости security ↔ tools.
    """
    # 1. Явный single-source mode.
    if isinstance(config_dict, dict):
        raw = config_dict.get("skills_data_path")
        if isinstance(raw, str) and raw.strip():
            return os.path.abspath(raw.strip())
    # 2. Three-layer mode → system root (<project_root>/skills).
    try:
        from tools.skills.registry import _system_skills_root  # type: ignore
        return os.path.abspath(_system_skills_root())
    except Exception:
        return None


@dataclass(frozen=True)
class ReadGuardConfig:
    """Runtime-конфиг read_guard (parsed out of config_dict)."""
    enabled: bool = True
    mode: str = "deny"
    allowed_read_roots: list[str] = field(default_factory=list)
    allowed_read_paths: list[str] = field(default_factory=list)
    denied_read_paths: list[str] = field(default_factory=list)
    denied_file_patterns: list[str] = field(default_factory=list)
    denied_extensions: list[str] = field(default_factory=list)


class ReadGuardViolation(ValueError):
    """Исключение path-policy чтения. Envelope в self.error."""
    def __init__(
        self,
        *,
        path: str,
        reason: str,
        matched_rule: str | None = None,
        mode: str = "deny",
    ) -> None:
        self.error: dict[str, Any] = {
            "type": "read_guard_violation",
            "path": path,
            "operation": "read",
            "reason": reason,
            "mode": mode,
        }
        if matched_rule is not None:
            self.error["matched_rule"] = matched_rule
        super().__init__(reason)


def parse_read_guard_config(config_dict: dict[str, Any]) -> ReadGuardConfig:
    """
    Достаёт read_guard блок из top-level `config_dict["read_guard"]`.

    Safe-by-default-OFF: read_guard включается ТОЛЬКО явным `enabled: true`.
    Если ключа `read_guard` нет вообще — слой выключен. Это симметрично с
    `write_guard` и `url_policy` (см. resolve_tool_context в path_policy.py).
    """
    raw = config_dict.get("read_guard") or {}
    if not isinstance(raw, dict):
        raw = {}
    return ReadGuardConfig(
        enabled=bool(raw.get("enabled", False)),
        mode=str(raw.get("mode", "deny")).strip().lower(),
        allowed_read_roots=list(raw.get("allowed_read_roots") or []),
        allowed_read_paths=list(raw.get("allowed_read_paths") or []),
        denied_read_paths=list(raw.get("denied_read_paths") or []),
        denied_file_patterns=list(raw.get("denied_file_patterns") or []),
        denied_extensions=list(raw.get("denied_extensions") or []),
    )


def expand_path_placeholders(raw: str, launch_root: str) -> str:
    """
    Резолвит плейсхолдеры в путях из конфига:
      ~              → home директория ($HOME, через os.path.expanduser);
      %USERPROFILE%  → переменная окружения USERPROFILE (Windows-friendly);
      <launch_root>  → переданный launch_root.
    Также через expandvars подставляются прочие env-переменные ($FOO, %FOO%).

    Возвращает абсолютный нормализованный путь после `realpath` — это
    критично для симметрии с `_resolve_candidate`, иначе на macOS пары
    типа /tmp vs /private/tmp не матчатся (symlink chain).
    """
    expanded = raw.replace("<launch_root>", launch_root)
    expanded = os.path.expanduser(expanded)
    expanded = os.path.expandvars(expanded)
    absolute = os.path.abspath(expanded)
    try:
        return os.path.realpath(absolute, strict=False)
    except TypeError:
        return os.path.realpath(absolute)


def _is_within(candidate_abs: str, root_abs: str, *, case_insensitive: bool) -> bool:
    """True если candidate лежит под root (по os.path.commonpath)."""
    a = candidate_abs.casefold() if case_insensitive else candidate_abs
    b = root_abs.casefold() if case_insensitive else root_abs
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:
        # Разные drive letters на Windows или пустые пути.
        return False


def _allow_by_default_wildcard(allowed_read_roots: list[str]) -> bool:
    return any(
        isinstance(r, str) and r.strip().lower() in _GLOB_WILDCARD_MARKERS
        for r in allowed_read_roots
    )


def _resolve_candidate(raw_path: str, launch_root: str) -> str:
    """
    Резолвит путь, который запросил инструмент:
      - пустой → ValueError;
      - относительный → относительно launch_root;
      - абсолютный → как есть;
      - применяется expanduser/expandvars;
      - resolve symlinks через realpath (для существующих путей).
    Возвращает абсолютный нормализованный путь.
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("path must be non-empty string")
    expanded = os.path.expanduser(raw_path)
    expanded = os.path.expandvars(expanded)
    if not os.path.isabs(expanded):
        expanded = os.path.join(launch_root, expanded)
    absolute = os.path.abspath(expanded)
    # realpath для защиты от symlink-обходов. Если путь не существует —
    # резолвится тот же путь без ошибки (Python 3.10+ poses strict=False).
    try:
        return os.path.realpath(absolute, strict=False)
    except TypeError:
        # strict kwarg добавлен в 3.10; для совместимости:
        return os.path.realpath(absolute)


def check_read_path_allowed(
    raw_path: str,
    *,
    launch_root: str,
    cfg: ReadGuardConfig,
    case_insensitive: bool | None = None,
    extra_allowed_paths: list[str] | None = None,
) -> str:
    """
    Главная проверка path-policy. Возвращает абсолютный нормализованный путь
    при успехе, иначе бросает `ReadGuardViolation`.

    Порядок (см. read-guard-spec.md «Логика проверки»):
      1. resolve кандидат-путь (absolute + realpath);
      2. allowed_read_paths exception — если путь матчится, разрешаем;
      3. denied_read_paths — блокируем;
      4. denied_file_patterns (по basename, fnmatch);
      5. denied_extensions;
      6. allowed_read_roots (если нет "*" — путь должен быть под одним из корней).
    """
    if case_insensitive is None:
        case_insensitive = os.name == "nt"

    candidate = _resolve_candidate(raw_path, launch_root)

    # 2. allowed_read_paths override — если путь явно разрешён, пропускаем
    #    остальные проверки denied_*. (allowed_read_roots по-прежнему может
    #    отрезать, см. шаг 6 — поэтому override применяется ВНУТРИ denied_*.)
    #    `extra_allowed_paths` — программно добавленные exceptions (например,
    #    путь до скиллов из get_skills_registry), не из конфига.
    cfg_paths = list(cfg.allowed_read_paths)
    if extra_allowed_paths:
        cfg_paths.extend(extra_allowed_paths)
    allowed_overrides_paths = [
        expand_path_placeholders(p, launch_root) for p in cfg_paths
    ]
    in_allow_override = any(
        _is_within(candidate, allowed, case_insensitive=case_insensitive)
        for allowed in allowed_overrides_paths
    )

    # Если путь явно разрешён (allowed_read_paths или extra_allowed_paths,
    # например путь до skills) — это полный пропуск всех остальных шагов,
    # включая allowed_read_roots. Иначе скиллы, лежащие вне launch_root,
    # блокировались бы шагом 6.
    if in_allow_override:
        return candidate

    # 3. denied_read_paths
    for raw_denied in cfg.denied_read_paths:
        denied_abs = expand_path_placeholders(raw_denied, launch_root)
        if _is_within(candidate, denied_abs, case_insensitive=case_insensitive):
            raise ReadGuardViolation(
                path=candidate,
                reason=f"path is within denied root: {denied_abs}",
                matched_rule=raw_denied,
                mode=cfg.mode,
            )

    # 4. denied_file_patterns (basename match)
    basename = os.path.basename(candidate)
    for pattern in cfg.denied_file_patterns:
        if fnmatch.fnmatch(basename, pattern):
            raise ReadGuardViolation(
                path=candidate,
                reason=f"file name matches denied pattern: {pattern}",
                matched_rule=pattern,
                mode=cfg.mode,
            )

    # 5. denied_extensions
    _, ext = os.path.splitext(candidate)
    if ext:
        ext_norm = ext.casefold() if case_insensitive else ext
        for raw_ext in cfg.denied_extensions:
            target = raw_ext.casefold() if case_insensitive else raw_ext
            if ext_norm == target:
                raise ReadGuardViolation(
                    path=candidate,
                    reason=f"file extension is denied: {raw_ext}",
                    matched_rule=raw_ext,
                    mode=cfg.mode,
                )

    # 6. allowed_read_roots — последний барьер.
    if _allow_by_default_wildcard(cfg.allowed_read_roots):
        # "*"/"all" → allow-by-default по ФС, denied_* уже пройдены выше.
        return candidate

    roots = cfg.allowed_read_roots or [launch_root]
    roots_abs = [expand_path_placeholders(r, launch_root) for r in roots]
    if any(_is_within(candidate, r, case_insensitive=case_insensitive) for r in roots_abs):
        return candidate

    raise ReadGuardViolation(
        path=candidate,
        reason=f"path is outside allowed_read_roots: {roots_abs}",
        matched_rule="allowed_read_roots",
        mode=cfg.mode,
    )


def _default_blocked_envelope(tool_name: str, error: dict[str, Any]) -> str:
    """
    Дефолтный envelope для блокировки read_guard'ом. Включает поле `status`,
    которое нужно тест‑валидаторам `must_contain: ["tool", "status"]`.
    """
    return json.dumps(
        {
            "tool": tool_name,
            "ok": False,
            "status": "blocked",
            "error": error,
        },
        ensure_ascii=False,
    )


def _read_guard_blocked_result(
    func: Callable[..., Any],
    error: dict[str, Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    blocked_result_factory: Callable[[dict[str, Any]], str] | None,
) -> str:
    """
    Резолвит envelope для блокированного read-вызова в порядке:
      1. явный `blocked_result_factory` (если передан в декоратор);
      2. `_read_guard_blocked_result` метод у тула (если есть);
      3. дефолтный envelope с tool/ok/status/error.
    """
    if blocked_result_factory is not None:
        return blocked_result_factory(error)
    self_obj = args[0] if args else None
    tool_method = getattr(self_obj, "_read_guard_blocked_result", None)
    if callable(tool_method):
        return tool_method(error, *args[1:], **kwargs)
    return _default_blocked_envelope(_tool_name(func, args), error)


def enforce_read_guard(
    path_arg: str | None = None,
    path_extractor: Callable[..., str] | None = None,
    blocked_result_factory: Callable[[dict[str, Any]], str] | None = None,
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """
    Декоратор для read-тулов. По смыслу и интерфейсу зеркалит `enforce_write_guard`.

    Применяется к `read_file`, `list_files`, `search_files` (см. spec).
    Не применяется к write-тулам (write_guard), execute-тулам (command_moderation)
    и url-тулам (url_policy).
    """
    def decorator(func: Callable[..., str]) -> Callable[..., str]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            context = resolve_tool_context(*args, **kwargs)
            config_dict = _config_from_context(context)
            cfg = parse_read_guard_config(config_dict)

            # Floor carve-out (Spec B §6): runs BEFORE cfg.enabled check so
            # operational .gc reads are denied even when read_guard is off.
            # skills/instructions/none fall through to the normal flow.
            from security.protected_paths import classify_path, protected_paths, protection_enabled
            if protection_enabled(config_dict):
                try:
                    cand = _extract_named_value(func, args, kwargs, path_arg, path_extractor, "path")
                    ps = protected_paths(cwd=context.launch_root)
                    if classify_path(cand, ps) == "operational":
                        err = {
                            "type": "protected_path_denied",
                            "path": cand,
                            "operation": "read",
                            "message": "Operational .gc is read-protected.",
                        }
                        audit_policy_decision(
                            context,
                            tool_name=_tool_name(func, args),
                            decision="blocked",
                            reason_code="protected_read_deny",
                        )
                        return _read_guard_blocked_result(
                            func, err, args, kwargs,
                            blocked_result_factory=blocked_result_factory,
                        )
                except ValueError:
                    pass

            if not cfg.enabled:
                audit_policy_decision(
                    context,
                    tool_name=_tool_name(func, args),
                    decision="executed",
                    reason_code="policy_disabled",
                )
                return func(*args, **kwargs)

            try:
                candidate_path = _extract_named_value(
                    func, args, kwargs, path_arg, path_extractor, "path",
                )
            except ValueError:
                # Аргумент path отсутствует/некорректен — пусть сам тул вернёт
                # его validation error.
                return func(*args, **kwargs)

            skills_root = _resolve_skills_root(config_dict)
            extra_allowed = [skills_root] if skills_root else None
            try:
                check_read_path_allowed(
                    candidate_path,
                    launch_root=context.launch_root,
                    cfg=cfg,
                    extra_allowed_paths=extra_allowed,
                )
            except ReadGuardViolation as exc:
                mode = (cfg.mode or "deny").strip().lower()
                if mode == "allow":
                    # Документированный пропуск: контент возвращается, но в audit
                    # помечаем что путь был вне allowed_read_*.
                    audit_policy_decision(
                        context,
                        tool_name=_tool_name(func, args),
                        decision="executed",
                        reason_code="policy_allow_out_of_perimeter",
                        risk_flags=["out_of_perimeter"],
                    )
                    return func(*args, **kwargs)
                if mode == "ask":
                    from security.confirm_port import request_confirm
                    _rg_path = exc.error.get("path", candidate_path)
                    confirmed = request_confirm(
                        title="Защита чтения",
                        question=f"Разрешить чтение вне периметра проекта: {_rg_path}?",
                        fields=[
                            ("Tool", _tool_name(func, args)),
                            ("Operation", "read"),
                            ("Path", _rg_path),
                            ("Reason", exc.error.get("reason", "")),
                        ],
                        ctx=context.ctx,
                        decision_key=f"read:{_rg_path}",
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
                    exc.error["type"] = "read_guard_cancelled_by_user"
                    exc.error["message"] = (
                        f"User declined the read of '{_rg_path}'. Do NOT retry the same "
                        "read, and do NOT silently read a different file — ask the user or stop."
                    )
                audit_policy_decision(
                    context,
                    tool_name=_tool_name(func, args),
                    decision="blocked",
                    reason_code="policy_deny" if mode == "deny" else "policy_ask_cancelled",
                    error_type=exc.error.get("type"),
                )
                return _read_guard_blocked_result(
                    func, exc.error, args, kwargs,
                    blocked_result_factory=blocked_result_factory,
                )
            except ValueError as exc:
                # Пустой path и т.п. — отдадим обычный validation error через тул.
                _ = exc
                return func(*args, **kwargs)

            audit_policy_decision(
                context,
                tool_name=_tool_name(func, args),
                decision="executed",
                reason_code="policy_allow",
            )
            return func(*args, **kwargs)
        return wrapper
    return decorator


def _config_from_context(context: Any) -> dict[str, Any]:
    """Достаёт config_dict из ToolSecurityContext или его ctx."""
    ctx = getattr(context, "ctx", None)
    cd = getattr(ctx, "config_dict", None)
    if isinstance(cd, dict):
        return cd
    return {}


__all__ = [
    "ReadGuardConfig",
    "ReadGuardViolation",
    "check_read_path_allowed",
    "enforce_read_guard",
    "expand_path_placeholders",
    "parse_read_guard_config",
]
