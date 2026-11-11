"""Фикстуры тестов жизненного цикла MCP; тест-модуль импортирует нужные.

* ``mcp_home`` — свой ``GC_HOME`` на тест: каталог тулов и журнал процессов
  не делятся между тестами.
* ``mcp_owner`` — свой владелец сессий на тест: реестры сессий процессные, и
  сессия прошлого теста под тем же именем досталась бы следующему. В конце
  закрывает все сессии владельца в трёх реестрах.
* ``mcp_reaper`` — после теста добивает каждый процесс из журналов под
  ``tmp_path`` и закрывает объекты, переданные в ``track``.

``mcp_owner`` и ``mcp_reaper`` асинхронные: сессии закрываются на цикле
теста, где их открывали.
"""

from __future__ import annotations

import contextlib
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from gc_core.config_modules import env_vars
from gc_core.mcp_session_owner import new_owner_token, use_owner
from gc_core.tools.connect_mcp import client as mcp_client
from gc_core.tools.connect_mcp import stream_client
from gc_core.tools.connect_mcp.persistent_stdio import PersistentStdioRegistry

from gc_testkit.mcp import alive, wait_for

#: Сколько ждать, пока добитые процессы исчезнут.
_REAP_SEC = 5.0


@pytest.fixture
def mcp_home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "gc"
    home.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", home)
    return home


async def _close_owner_sessions(owner: str) -> None:
    await PersistentStdioRegistry.instance().aclose_all()
    # Через модуль, а не по имени: тесты подменяют `_SESSION_MANAGER`.
    await stream_client._SESSION_MANAGER.aclose_all()
    for key in [k for k in mcp_client._MCP_SESSIONS if k[0] == owner]:
        mcp_client._MCP_SESSIONS.pop(key, None)


@pytest.fixture
async def mcp_owner() -> AsyncIterator[str]:
    with use_owner(new_owner_token("mcp-test")) as owner:
        try:
            yield owner
        finally:
            await _close_owner_sessions(owner)


def _logged_pids(root: Path) -> list[int]:
    pids = []
    for log in root.rglob("*.log"):
        for line in log.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in ("start", "child"):
                pids.append(int(parts[1]))
    return pids


def _kill(pid: int) -> None:
    """SIGKILL группе, если процесс — её лидер, иначе только ему.

    Разовый stdio живёт в группе pytest: групповой сигнал по нему убил бы
    сам прогон.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)


class McpReaper:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._tracked: list[Any] = []

    def track(self, obj: Any) -> Any:
        """Закрыть ``obj`` (``aclose`` или ``close``) при уборке."""
        self._tracked.append(obj)
        return obj

    async def reap(self) -> None:
        """Закрыть отданное в ``track``, добить процессы из журналов."""
        while self._tracked:
            obj = self._tracked.pop()
            with contextlib.suppress(Exception):
                closing = getattr(obj, "aclose", None)
                if closing is not None:
                    await closing()
                else:
                    obj.close()
        pids = [pid for pid in _logged_pids(self._root) if alive(pid)]
        for pid in pids:
            _kill(pid)
        await wait_for(
            lambda: not any(alive(pid) for pid in pids), timeout=_REAP_SEC
        )


@pytest.fixture
async def mcp_reaper(tmp_path) -> AsyncIterator[McpReaper]:
    reaper = McpReaper(tmp_path)
    try:
        yield reaper
    finally:
        await reaper.reap()
