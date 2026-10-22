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
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gc_core.plugins.manifest import HookCommand, Plugin

logger = logging.getLogger(__name__)

_DENY_WORDS = frozenset({"deny", "block"})
_bash_warned = False


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


def _find_bash() -> str | None:
    """Оболочка для хуков: ``/bin/bash`` в POSIX, иначе bash из PATH."""
    if sys.platform != "win32" and os.path.exists("/bin/bash"):
        return "/bin/bash"
    return shutil.which("bash")


def _warn_no_bash_once() -> None:
    global _bash_warned
    if not _bash_warned:
        _bash_warned = True
        logger.warning("plugin hooks need bash in PATH")


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Убить хук вместе с потомками и дождаться завершения."""
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    await proc.wait()


async def run_hook(
    plugin: Plugin,
    hook: HookCommand,
    payload: dict[str, Any],
    *,
    cwd: str,
    default_timeout_sec: int,
) -> HookResult:
    shell = _find_bash()
    if shell is None:
        _warn_no_bash_once()
        return HookResult(plugin=plugin.name, failed=True)
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
            shell,
            "-c",
            hook.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=workdir,
            start_new_session=sys.platform != "win32",
        )
    except OSError as exc:
        logger.warning("plugin %s hook did not start: %s", plugin.name, exc)
        return HookResult(plugin=plugin.name, failed=True)
    try:
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
        await _terminate(proc)
        return HookResult(plugin=plugin.name, failed=True)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(_terminate(proc))
        except Exception:
            logger.debug("plugin hook cleanup failed", exc_info=True)
        raise
    except OSError as exc:
        logger.warning("plugin %s hook failed: %s", plugin.name, exc)
        await _terminate(proc)
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
