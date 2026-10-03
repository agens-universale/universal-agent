"""Serializable worker facts consumed by the native security guard flows."""

from __future__ import annotations

import hashlib
import json
import os
from contextvars import ContextVar
from dataclasses import asdict, replace
from typing import Any

from gc_core.execution.guard_contracts import (
    GuardProbeCheck,
    GuardProbeRequest,
    GuardProbeResult,
)

active_guard_checks: ContextVar[list[GuardProbeCheck] | None] = ContextVar(
    "active_guard_checks",
    default=None,
)


def evaluate_guard_probe(
    request: GuardProbeRequest,
    *,
    config_dict: dict[str, Any],
) -> GuardProbeResult:
    """Evaluate host facts with existing policy helpers on the effect worker.

    Only policy violations become data. Infrastructure failures propagate;
    the workflow must never interpret an unavailable probe as permission.
    """
    if request.schema_version != 1 or not request.cwd:
        raise ValueError("Guard probe requires schema 1 and an explicit cwd")
    from gc_core.security.path_policy import (
        WriteGuardViolation,
        check_write_path_allowed,
    )
    from gc_core.security.protected_paths import (
        classify_path,
        protected_decision,
        protected_paths,
    )
    from gc_core.security.read_guard import (
        ReadGuardViolation,
        _resolve_skills_root,
        check_read_path_allowed,
        parse_read_guard_config,
    )

    result = GuardProbeResult()
    if request.kind == "write_path":
        try:
            path = check_write_path_allowed(
                request.value,
                request.cwd,
                _write_guard_extra_paths(config_dict),
                base_dir=request.cwd,
                operation=request.operation or "write",
            )
            result = replace(result, canonical_path=str(path))
        except WriteGuardViolation as exc:
            result = replace(result, violation=exc.error)
    elif request.kind == "read_path":
        skills_root = _resolve_skills_root(config_dict)
        try:
            path = check_read_path_allowed(
                request.value,
                launch_root=request.cwd,
                cfg=parse_read_guard_config(config_dict),
                extra_allowed_paths=[skills_root] if skills_root else None,
            )
            result = replace(result, canonical_path=str(path))
        except ReadGuardViolation as exc:
            result = replace(result, violation=exc.error)
    elif request.kind == "protected_write":
        result = replace(
            result,
            protected_decision=protected_decision(
                request.value,
                cwd=request.cwd,
            ),
        )
    elif request.kind == "protected_read":
        category = classify_path(
            request.value, protected_paths(cwd=request.cwd)
        )
        result = replace(
            result,
            protected_decision=(
                "deny" if category == "operational" else "allow"
            ),
        )
    elif request.kind == "command":
        from gc_core.security.command_guard import scan_command_for_protected
        from gc_core.security.protected_paths import protected_extra_paths

        result = replace(
            result,
            protected_decision=scan_command_for_protected(
                request.value,
                cwd=request.cwd,
                extra=protected_extra_paths(config_dict) or None,
            ),
        )
    elif request.kind == "download_destination":
        result = _download_destination(request, config_dict)
    elif request.kind == "command_prompts":
        from gc_core.security.command_moderation import (
            load_command_judge_prompt,
        )

        result = replace(
            result,
            data={
                "system": load_command_judge_prompt("system"),
                "user": load_command_judge_prompt("user"),
            },
        )
    elif request.kind == "command_host":
        result = _command_host(request)
    else:
        raise ValueError(f"Unknown guard probe kind: {request.kind}")
    payload = {"request": asdict(request), "result": asdict(result)}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return replace(result, facts_digest=digest)


def _write_guard_extra_paths(config_dict: Any) -> list[str]:
    from gc_core.settings import settings_of

    return list(settings_of(config_dict).security.write_guard.extra_paths)


def _command_host(request):
    from gc_core.constants.tool_names import EXEC_PYTHON_NAME
    from gc_core.security.command_moderation import load_never_touch_paths
    from gc_core.utils.project_data import os_label

    cwd = request.value.strip() or (
        request.cwd if request.tool_name == EXEC_PYTHON_NAME else os.getcwd()
    )
    canonical_path = os.path.abspath(cwd)
    return GuardProbeResult(
        canonical_path=canonical_path,
        os_name=os_label(),
        shell=os.environ.get("SHELL") or os.environ.get("COMSPEC") or "",
        has_venv=bool(os.environ.get("VIRTUAL_ENV")),
        data={"never_touch_paths": load_never_touch_paths(canonical_path)},
    )


def _download_destination(request, config_dict):
    from gc_core.tools.exec.download_file import DownloadFile

    tool = DownloadFile(config_dict=config_dict)
    validated = tool._validate_filename(request.value)
    if isinstance(validated, dict):
        return GuardProbeResult(data={"error": validated})
    filename, extension = validated
    settings = tool._resolve_download_settings(request.cwd)
    if isinstance(settings, dict):
        return GuardProbeResult(data={"error": settings})
    root, max_bytes = settings
    return GuardProbeResult(
        canonical_path=os.path.join(root, filename),
        data={
            "download_root": root,
            "max_bytes": max_bytes,
            "filename": filename,
            "extension": extension,
        },
    )


def probe_step(ctx: Any, request: GuardProbeRequest):
    """Yield a serializable request; transport never sees the guard generator."""
    from gc_core.security.dual_wrapper import AwaitOnCaller

    async def collect():
        result = await ctx.guard_probe(request)
        if result.schema_version != 1 or not result.facts_digest:
            raise ValueError("Invalid guard probe result")
        if (
            request.kind.startswith("protected_") or request.kind == "command"
        ) and result.protected_decision not in {"allow", "ask", "deny"}:
            raise ValueError("Invalid protected-path decision")
        checks = active_guard_checks.get()
        if checks is not None:
            checks.append(GuardProbeCheck(request, result))
        return result

    return AwaitOnCaller(collect)
