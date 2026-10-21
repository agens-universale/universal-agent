"""Запуск хука плагина как внешней команды.

Контракт (один, без угадывания агента по окружению):

* В окружении всегда заданы ``GC_PLUGIN_ROOT`` и ``CLAUDE_PLUGIN_ROOT`` (путь
  к каталогу плагина) и ``GC_PROJECT_DIR`` (рабочий каталог). В самой команде
  ``${CLAUDE_PLUGIN_ROOT}`` подставляет оболочка.
* На stdin приходит JSON события (``hook_event_name``, ``cwd``, для инструментов
  ``tool_name`` и ``tool_input``).
* Ответ: код 0 и необязательный JSON на stdout; код 2 означает отказ, причина
  берётся из stderr. Любой другой код и таймаут считаются сбоем хука и не
  останавливают агента (fail-open), в лог уходит предупреждение.
* Понимаются оба формата контекста: ``additionalContext`` верхнего уровня и
  ``hookSpecificOutput.additionalContext``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gc_core.plugins.manifest import HookCommand, Plugin

logger = logging.getLogger(__name__)

_DENY_WORDS = frozenset({"deny", "block"})


@dataclass(frozen=True)
class HookResult:
    plugin: str
    blocked: bool = False
    reason: str = ""
    additional_context: str = ""
    updated_input: dict[str, Any] | None = None
    failed: bool = False


def _parse_stdout(plugin: str, stdout: str) -> HookResult:
    text = stdout.strip()
    if not text:
        return HookResult(plugin=plugin)
    try:
        data = json.loads(text)
    except ValueError:
        return HookResult(plugin=plugin)
    if not isinstance(data, dict):
        return HookResult(plugin=plugin)
    specific = data.get("hookSpecificOutput")
    specific = specific if isinstance(specific, dict) else {}
    context = specific.get("additionalContext", data.get("additionalContext"))
    decision = str(
        specific.get("permissionDecision", data.get("decision", ""))
    ).lower()
    reason = str(
        specific.get("permissionDecisionReason", data.get("reason", ""))
    )
    updated = specific.get("updatedInput", data.get("updatedInput"))
    return HookResult(
        plugin=plugin,
        blocked=decision in _DENY_WORDS,
        reason=reason,
        additional_context=context if isinstance(context, str) else "",
        updated_input=updated if isinstance(updated, dict) else None,
    )


async def run_hook(
    plugin: Plugin,
    hook: HookCommand,
    payload: dict[str, Any],
    *,
    cwd: str,
    default_timeout_sec: int,
) -> HookResult:
    env = {
        **os.environ,
        "GC_PLUGIN_ROOT": str(plugin.root),
        "CLAUDE_PLUGIN_ROOT": str(plugin.root),
        "GC_PROJECT_DIR": cwd,
    }
    timeout = hook.timeout_sec or default_timeout_sec
    workdir = cwd if Path(cwd).is_dir() else str(plugin.root)
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-c",
            hook.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=workdir,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(json.dumps(payload).encode("utf-8")),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning(
            "plugin %s hook timed out after %ss: %s",
            plugin.name,
            timeout,
            hook.command,
        )
        try:
            proc.kill()
        except (ProcessLookupError, UnboundLocalError):
            pass
        return HookResult(plugin=plugin.name, failed=True)
    except OSError as exc:
        logger.warning("plugin %s hook did not start: %s", plugin.name, exc)
        return HookResult(plugin=plugin.name, failed=True)

    if proc.returncode == 2:
        reason = stderr.decode("utf-8", "replace").strip() or "blocked"
        return HookResult(plugin=plugin.name, blocked=True, reason=reason)
    if proc.returncode != 0:
        logger.warning(
            "plugin %s hook exited with %s: %s",
            plugin.name,
            proc.returncode,
            stderr.decode("utf-8", "replace").strip()[:300],
        )
        return HookResult(plugin=plugin.name, failed=True)
    return _parse_stdout(plugin.name, stdout.decode("utf-8", "replace"))
