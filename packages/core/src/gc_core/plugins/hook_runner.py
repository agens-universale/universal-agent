"""Запуск хука плагина как внешней команды.

Контракт (один, без угадывания агента по окружению):

* В окружении всегда заданы ``GC_PLUGIN_ROOT`` и ``CLAUDE_PLUGIN_ROOT`` (путь
  к каталогу плагина) и ``GC_PROJECT_DIR`` (рабочий каталог). Команда хука в
  формате — строка оболочки, её исполняет ``bash -c``; ``${CLAUDE_PLUGIN_ROOT}``
  в ней подставляет оболочка.
* На stdin приходит JSON события (``hook_event_name``, ``cwd`` и поля
  события).
* Ответ: код 0 и stdout. JSON-объект даёт ``additionalContext`` (верхнего
  уровня или в ``hookSpecificOutput``) и ``decision: block`` с ``reason``;
  не-JSON — весь текст идёт в контекст. Код 2 означает отказ, причина
  берётся из stderr. Любой другой код и таймаут — сбой хука, агент не
  останавливается (fail-open), в лог уходит предупреждение.
* Контекст и причина отказа ограничены ``MAX_CONTEXT_CHARS``, длиннее —
  обрезаются с пометкой.
* Стоп хода убивает идущий хук (в POSIX — всю группу его процессов), а
  следующий хук не запускает. Это не сбой: результат пустой, ход
  кончается стопом.
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
from gc_core.user_stop import UserStopInterrupt, current_user_stop

logger = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 10_000
_TRUNCATED_MARK = "\n[вывод хука обрезан до 10 000 символов]"
#: Сколько ждать выхода убитого хука, прежде чем закрыть его трубы.
_EXIT_GRACE_SEC = 2.0
_bash_warned = False


@dataclass(frozen=True)
class HookResult:
    plugin: str
    blocked: bool = False
    reason: str = ""
    additional_context: str = ""
    failed: bool = False


def _bounded(text: str) -> str:
    if len(text) <= MAX_CONTEXT_CHARS:
        return text
    return text[:MAX_CONTEXT_CHARS] + _TRUNCATED_MARK


def _parse_stdout(plugin: str, stdout: str) -> HookResult:
    text = stdout.strip()
    if not text:
        return HookResult(plugin=plugin)
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return HookResult(plugin=plugin, additional_context=_bounded(text))
    specific = data.get("hookSpecificOutput")
    specific = specific if isinstance(specific, dict) else {}
    context = specific.get("additionalContext", data.get("additionalContext"))
    return HookResult(
        plugin=plugin,
        blocked=str(data.get("decision", "")).lower() == "block",
        reason=_bounded(str(data.get("reason", ""))),
        additional_context=(
            _bounded(context) if isinstance(context, str) else ""
        ),
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
    """Убить хук вместе с потомками и дождаться завершения.

    ``wait()`` ждёт ещё и EOF труб, а их держит потомок, ушедший из группы
    (``setsid``; на Windows — любой потомок bash). Тогда после короткой
    грации трубы закрываются: ни стоп, ни таймаут, ни отмена на нём не
    висят."""
    if not (sys.platform == "win32" and proc.returncode is not None):
        _kill(proc)
    exited = False
    try:
        await asyncio.wait_for(proc.wait(), _EXIT_GRACE_SEC)
        exited = True
    except TimeoutError:
        pass
    finally:
        if not exited:
            _close_pipes(proc)


def _kill(proc: asyncio.subprocess.Process) -> None:
    try:
        if sys.platform == "win32":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _close_pipes(proc: asyncio.subprocess.Process) -> None:
    """Закрыть наши концы труб хука — ``communicate`` получит EOF.
    Транспорт у asyncio приватный: нет его — закрывать нечего."""
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    try:
        transport.close()
    except OSError:
        # Трубы уже закрыты; не вышло только добить живой процесс (EPERM).
        pass


def _stop_requested() -> bool:
    stop = current_user_stop()
    return stop is not None and stop.requested


async def _acommunicate(
    proc: asyncio.subprocess.Process, data: bytes, timeout: int
) -> tuple[bytes, bytes] | None:
    """Обмен с хуком под таймаутом; ``None`` — хук снят стопом хода."""
    stop = current_user_stop()
    if stop is None:
        return await asyncio.wait_for(proc.communicate(data), timeout)
    armed = None
    try:
        async with stop.arm(
            kind="hook", release=lambda: _terminate(proc)
        ) as armed:
            out = await asyncio.wait_for(proc.communicate(data), timeout)
    except UserStopInterrupt:
        await _terminate(proc)
        return None
    except TimeoutError:
        # Стоп успел раньше таймаута: хук снят, это не сбой.
        if armed is None or not armed.fired:
            raise
        return None
    return None if armed.fired else out


async def run_hook(
    plugin: Plugin,
    hook: HookCommand,
    payload: dict[str, Any],
    *,
    cwd: str,
    default_timeout_sec: int,
) -> HookResult:
    if _stop_requested():
        return HookResult(plugin=plugin.name)
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
        exchanged = await _acommunicate(
            proc, json.dumps(payload).encode("utf-8"), timeout
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

    if exchanged is None:
        return HookResult(plugin=plugin.name)
    stdout, stderr = exchanged
    if proc.returncode == 2:
        reason = _bounded(
            stderr.decode("utf-8", "replace").strip() or "blocked"
        )
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
