"""Фикстуры тестов жизненного цикла MCP; тест-модуль импортирует нужные.

* ``mcp_home`` — свой ``GC_HOME`` на тест: каталог тулов и журнал процессов
  не делятся между тестами.
* ``mcp_owner`` — свой владелец сессий на тест: реестры сессий процессные, и
  сессия прошлого теста под тем же именем досталась бы следующему. В конце
  закрывает все сессии владельца в трёх реестрах.
* ``mcp_reaper`` — после теста закрывает объекты, переданные в ``track``, и
  добивает каждый процесс из журналов тестовых серверов под ``tmp_path``.

``mcp_owner`` и ``mcp_reaper`` асинхронные: сессии закрываются на цикле
теста, где их открывали. Каждое закрытие — с потолком ``_CLOSE_SEC``:
зависшее закрытие не вешает прогон, а падает ``AssertionError``.
"""

from __future__ import annotations

import asyncio
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

from gc_testkit.mcp import JOURNAL_SUFFIX, alive, children, launches, wait_for

#: Потолок одного закрытия: объекта из ``track`` или сессий владельца.
#: С запасом над бюджетом закрытия постоянного stdio (SIGTERM → 5 с → SIGKILL).
_CLOSE_SEC = 15.0
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
            try:
                await asyncio.wait_for(
                    _close_owner_sessions(owner), _CLOSE_SEC
                )
            except TimeoutError:
                raise AssertionError(
                    f"сессии владельца {owner} не закрылись за {_CLOSE_SEC} с"
                ) from None


def _logged_pids(root: Path) -> list[int]:
    """pid запусков и потомков из всех журналов тестовых серверов."""
    pids = []
    for path in root.rglob(f"*{JOURNAL_SUFFIX}"):
        control = Path(str(path)[: -len(JOURNAL_SUFFIX)])
        pids += launches(control) + children(control)
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


async def _close(obj: Any) -> None:
    closing = getattr(obj, "aclose", None)
    if closing is not None:
        await closing()
    else:
        # Синхронное закрытие — в потоке: иначе потолок его не прервёт.
        await asyncio.to_thread(obj.close)


class McpReaper:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._tracked: list[Any] = []

    def track(self, obj: Any) -> Any:
        """Закрыть ``obj`` (``aclose`` или ``close``) при уборке."""
        self._tracked.append(obj)
        return obj

    async def _close_tracked(self) -> list[str]:
        errors = []
        while self._tracked:
            obj = self._tracked.pop()
            try:
                await asyncio.wait_for(_close(obj), _CLOSE_SEC)
            except TimeoutError:
                errors.append(f"{obj!r}: закрытие дольше {_CLOSE_SEC} с")
            except Exception as exc:
                errors.append(f"{obj!r}: {exc!r}")
        return errors

    async def reap(self) -> None:
        """Закрыть отданное в ``track``, добить процессы из журналов.

        Процессы гасятся при любом исходе закрытий; ошибки и сроки
        собираются и в конце падают одним ``AssertionError``.
        """
        errors = await self._close_tracked()
        pids = [pid for pid in _logged_pids(self._root) if alive(pid)]
        for pid in pids:
            _kill(pid)
        try:
            await wait_for(
                lambda: not any(alive(pid) for pid in pids),
                timeout=_REAP_SEC,
            )
        except AssertionError as exc:
            errors.append(f"процессы не погасли: {exc}")
        if errors:
            raise AssertionError("уборка MCP: " + "; ".join(errors))


@pytest.fixture
async def mcp_reaper(tmp_path) -> AsyncIterator[McpReaper]:
    reaper = McpReaper(tmp_path)
    try:
        yield reaper
    finally:
        await reaper.reap()
