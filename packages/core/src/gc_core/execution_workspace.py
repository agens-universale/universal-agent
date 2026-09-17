"""Execution-environment workspace: root + I/O backend, assembled once per run."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from gc_core.backends.protocol import FsBackend


#: Потолок на ОДНУ попытку узнать рабочий каталог. У Harbor есть свой
#: внутренний (20 с), но он есть не у всякого бэкенда, а сборка контекста не
#: имеет права висеть на молчащем окружении неограниченно.
_CWD_PRIME_CEILING_SEC = 25.0


class WorkspaceRootUnknownError(RuntimeError):
    """Корень рабочей области не удалось узнать ни одним способом.

    Раньше на этом месте был молчаливый откат к ``/``: модель работала от
    корня файловой системы песочницы, и понять это можно было только по
    странным путям в ответах. Отказ громкий намеренно — корень рабочей области
    выбирается один раз и определяет ВСЁ остальное.
    """


#: Каталог, узнанный общим запасным путём. Слабые ключи обязательны: бэкенд
#: живёт один прогон, а обычный словарь держал бы его вечно — ровно та утечка,
#: что уже была на кэшах с ключом по объекту.
_PRIMED_CWD: "weakref.WeakKeyDictionary[Any, str]" = weakref.WeakKeyDictionary()


def _remember_primed_cwd(backend: Any, cwd: str) -> None:
    try:
        _PRIMED_CWD[backend] = cwd
    except TypeError:
        # Бэкенд без поддержки weakref (слоты, прокси) — кладём на него самого.
        try:
            backend._gc_primed_cwd = cwd
        except Exception:  # noqa: BLE001 — кэш не обязателен для корректности
            pass


def _primed_cwd(backend: Any) -> Optional[str]:
    try:
        cached = _PRIMED_CWD.get(backend)
    except TypeError:
        cached = None
    return cached or getattr(backend, "_gc_primed_cwd", None)


def _cwd_from_pwd(res: Any) -> Optional[str]:
    """Разобрать ответ ``pwd``: абсолютный путь при нулевом коде возврата."""
    lines = (getattr(res, "stdout", "") or "").strip().splitlines()
    if getattr(res, "return_code", 1) == 0 and lines and lines[0].startswith("/"):
        return lines[0]
    return None


@dataclass(frozen=True)
class ExecutionWorkspace:
    """Project root in the coordinates of the environment that executes tools.

    ``root`` is absolute in that environment (host path locally, container path
    in sandbox). ``backend`` is the only I/O port for facts and tool ops there.
    ``os_name`` / ``shell`` are env-block labels for that environment.
    ``root_writable`` — whether the project root accepts writes (sandbox: yes;
    local: one probe at assemble time).
    """

    root: str
    backend: FsBackend
    shell: str
    os_name: str
    root_writable: bool


def discover_backend_cwd(backend: FsBackend) -> str:
    """Working directory of the environment the backend talks to.

    ``default_cwd()`` may be None (container owns its WORKDIR); ask the
    environment itself, then fall back to ``/``.
    """
    default_cwd = getattr(backend, "default_cwd", None)
    declared = default_cwd() if callable(default_cwd) else None
    if declared:
        return declared
    # То, что узнал общий запасной путь: у async-only бэкенда своего места для
    # ответа нет, а синхронные сборщики рабочей области спрашивают отсюда.
    primed = _primed_cwd(backend)
    if primed:
        return primed
    exec_fn = getattr(backend, "exec", None)
    if not callable(exec_fn):
        return "/"
    try:
        res = exec_fn("pwd")
    except Exception:  # noqa: BLE001 — env may be down; callers still need a root
        return "/"
    lines = (getattr(res, "stdout", "") or "").strip().splitlines()
    if getattr(res, "return_code", 1) == 0 and lines and lines[0].startswith("/"):
        return lines[0]
    return "/"



async def aprime_backend_cwd(backend: Any, config: Optional[Dict[str, Any]] = None) -> None:
    """Узнать рабочий каталог окружения до сборки рабочей области.

    Песочнице рабочий каталог надо спросить у самого окружения, а это сеть —
    корутина. Сборка рабочей области синхронная, поэтому ответ получаем
    заранее: дальше его отдаёт ``default_cwd()`` или кэш ``_PRIMED_CWD``.

    Цепочка, по порядку убывания надёжности:

    1. бэкенд уже знает каталог (``default_cwd()`` или прошлый прогрев);
    2. свой прогрев бэкенда (``aprime_default_cwd``) под общим потолком;
    3. корень задан конфигом (``launch_root``/``project_root``) — спрашивать
       окружение незачем, отказа нет;
    4. НЕ песочница — корень возьмётся из ``os.getcwd()``, до ``/`` дело не
       доходит;
    5. общий ``pwd`` через корутинный exec-порт — он есть у любого бэкенда,
       в том числе у async-only, у которого синхронного ``exec`` нет вовсе;
    6. ничего не вышло — ``WorkspaceRootUnknownError``.

    Шестой пункт заменяет прежний молчаливый откат к ``/``. Цена ошибки
    несимметрична: лишний отказ сборки виден сразу, а корень ``/`` уводит весь
    прогон работать не там, и замечают это по странным путям в ответах.
    """
    default_cwd = getattr(backend, "default_cwd", None)
    if callable(default_cwd) and default_cwd():
        return
    if _primed_cwd(backend):
        return

    reasons: list[str] = []
    prime = getattr(backend, "aprime_default_cwd", None)
    if callable(prime):
        try:
            if await asyncio.wait_for(prime(), _CWD_PRIME_CEILING_SEC):
                return
            reasons.append("aprime_default_cwd вернул None")
        except Exception as exc:  # noqa: BLE001 — окружение может быть недоступно
            reasons.append(f"aprime_default_cwd: {type(exc).__name__}")

    if _config_root_raw(config):
        # Корень назван явно — он и победит в `resolve_execution_root`.
        return

    if not getattr(backend, "is_sandbox", False):
        # Локальный бэкенд корень берёт из `os.getcwd()`; `discover_backend_cwd`
        # для него не зовётся вовсе, так что отказ прогрева ничем не грозит —
        # но и растворяться он не должен.
        if reasons:
            logging.getLogger(__name__).warning(
                "backend_cwd_prime_failed: рабочий каталог окружения не узнан "
                "(%s; бэкенд: %s)",
                "; ".join(reasons),
                type(backend).__name__,
                extra={"event": "backend_cwd_prime_failed"},
            )
        return

    from gc_core.backends.async_protocol import arun_backend_exec_raw

    try:
        res = await asyncio.wait_for(
            arun_backend_exec_raw(backend, "pwd", timeout=_CWD_PRIME_CEILING_SEC),
            _CWD_PRIME_CEILING_SEC,
        )
        cwd = _cwd_from_pwd(res)
        if cwd:
            _remember_primed_cwd(backend, cwd)
            return
        reasons.append("pwd: пустой ответ или ненулевой код возврата")
    except Exception as exc:  # noqa: BLE001 — окружение может быть недоступно
        reasons.append(f"pwd: {type(exc).__name__}")

    logging.getLogger(__name__).warning(
        "backend_cwd_prime_failed: рабочий каталог окружения не узнан "
        "(%s; бэкенд: %s)",
        "; ".join(reasons),
        type(backend).__name__,
        extra={"event": "backend_cwd_prime_failed"},
    )
    raise WorkspaceRootUnknownError(
        "не удалось узнать рабочий каталог окружения ("
        + "; ".join(reasons)
        + "). Задайте корень явно: launch_root (project_root) в конфиге"
    )


def _config_root_raw(config: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(config, dict):
        return None
    raw = config.get("launch_root") or config.get("project_root")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def resolve_execution_root(
    backend: FsBackend,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """Resolve project root in execution-environment coordinates."""
    raw = _config_root_raw(config)
    if getattr(backend, "is_sandbox", False):
        if raw:
            # Already in container coordinates — do not host-abspath.
            norm = getattr(backend, "normpath", None)
            return norm(raw) if callable(norm) else (raw.rstrip("/") or "/")
        return discover_backend_cwd(backend)

    if raw:
        return os.path.abspath(os.path.expanduser(os.path.normpath(raw)))
    default_cwd = getattr(backend, "default_cwd", None)
    cwd = (default_cwd() if callable(default_cwd) else None) or os.getcwd()
    return os.path.abspath(cwd)


def resolve_execution_shell(backend: FsBackend) -> str:
    """Shell label for the execution environment (prompt / env-block)."""
    if getattr(backend, "is_sandbox", False):
        return "sh"
    shell = os.environ.get("SHELL", "unknown")
    if (not shell or shell == "unknown") and sys.platform == "win32":
        shell = os.environ.get("COMSPEC", "cmd.exe")
    return shell or "unknown"


def host_os_label() -> str:
    """Human-readable host OS from ``sys.platform`` (no ``platform.system()``/WMI)."""
    if sys.platform == "win32":
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("linux"):
        return "Linux"
    return sys.platform


def resolve_execution_os(backend: FsBackend) -> str:
    """OS label for the execution environment (prompt / env-block)."""
    if getattr(backend, "is_sandbox", False):
        return "Linux"
    return host_os_label()


def resolve_root_writable(backend: FsBackend, root: str) -> bool:
    """Whether ``root`` is writable in the execution environment."""
    if getattr(backend, "is_sandbox", False):
        return True
    from gc_core.utils.directory_write_access import directory_allows_probe_write

    return directory_allows_probe_write(root)


def build_execution_workspace(
    backend: FsBackend,
    config: Optional[Dict[str, Any]] = None,
) -> ExecutionWorkspace:
    """Assemble workspace once (typically in ``create_run_context``)."""
    root = resolve_execution_root(backend, config)
    return ExecutionWorkspace(
        root=root,
        backend=backend,
        shell=resolve_execution_shell(backend),
        os_name=resolve_execution_os(backend),
        root_writable=resolve_root_writable(backend, root),
    )


__all__ = [
    "ExecutionWorkspace",
    "WorkspaceRootUnknownError",
    "aprime_backend_cwd",
    "build_execution_workspace",
    "discover_backend_cwd",
    "host_os_label",
    "resolve_execution_os",
    "resolve_execution_root",
    "resolve_execution_shell",
    "resolve_root_writable",
]
