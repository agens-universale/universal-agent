"""Смерть сервера или истёкший срок посреди вызова — ошибка, а не пустой успех.

Сервер, умерший без ответа, даёт ``McpServerGone``; сервер, не ответивший
за срок, — ``McpCallTimedOut`` и ``notifications/cancelled``, сессия
остаётся. Автоповтор ни ту, ни другую не повторяет: вызов мог исполниться.
Под durable вместо них — ``McpSessionLostError``.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from gc_core.tools.connect_mcp import registry as reg
from gc_core.tools.connect_mcp.api import acall_mcp_tool
from gc_core.tools.connect_mcp.call_errors import (
    McpCallOutcomeUnknown,
    McpCallTimedOut,
    McpServerGone,
)
from gc_core.tools.connect_mcp.durable_scope import (
    McpSessionLostError,
    durable_mcp_calls,
)
from gc_core.tools.connect_mcp.persistent_stdio import (
    _NOT_RUNNING_MSG,
    PersistentStdioRegistry,
    PersistentStdioSession,
)
from gc_core.tools.connect_mcp.transport import forget_stdio_reader
from gc_core.utils import aretry
from gc_testkit.mcp import (
    FakeClock,
    call_tool,
    events,
    launches,
    set_behavior,
    stdio_cfg,
    wait_for,
)
from gc_testkit.mcp.fixtures import mcp_owner, mcp_reaper

pytestmark = pytest.mark.skipif(os.name != "posix", reason="процессы POSIX")

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 20.0


def _persistent(control) -> dict:
    return {**stdio_cfg(control), "persistent_session": True}


def _requests(control, method: str) -> list[str]:
    """Id запросов ``method`` из журнала сервера, по порядку."""
    prefix = f"req {method} "
    return [
        line[len(prefix) :]
        for line in events(control)
        if line.startswith(prefix)
    ]


async def _acall_with_retry(cfg: dict, tool: str) -> object:
    """Вызов через ``acall_mcp_tool`` с включённым автоповтором."""
    registry = {
        f"mcp_srv_{tool}": {
            "tool": {"parameters": {}},
            "metadata": {"mcp_server_key": "srv", "mcp_tool_name": tool},
        }
    }
    return await asyncio.wait_for(
        acall_mcp_tool(
            f"mcp_srv_{tool}",
            {},
            {"srv": {**cfg, "use_retry": True}},
            registry,
            timeout=5.0,
        ),
        _CEILING_SEC,
    )


async def test_death_mid_call_is_error_sent_once(
    tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    set_behavior(control)

    with pytest.raises(McpServerGone, match="завершился, не ответив"):
        await _acall_with_retry(_persistent(control), "crash")

    assert len(_requests(control, "tools/call")) == 1


async def test_timeout_is_error_with_cancel(tmp_path, mcp_reaper, mcp_owner):
    control = tmp_path / "s.json"
    set_behavior(control)
    cfg = _persistent(control)
    first = await call_tool("srv", cfg, "echo", {"text": "a"})

    with pytest.raises(McpCallTimedOut, match="не ответил за 1 с"):
        await call_tool("srv", cfg, "nap", {"seconds": 30}, timeout=1)

    [nap_id] = _requests(control, "tools/call")[1:]
    await wait_for(lambda: f"cancelled {nap_id}" in events(control))
    after = await call_tool("srv", cfg, "echo", {"text": "b"})
    assert after["pid"] == first["pid"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("nap", {"seconds": 30}), ("crash", {})],
    ids=["срок", "смерть"],
)
async def test_durable_gets_outcome_unknown(
    tmp_path, mcp_reaper, mcp_owner, tool, arguments
):
    control = tmp_path / "s.json"
    set_behavior(control)

    with durable_mcp_calls(), pytest.raises(McpSessionLostError):
        await call_tool(
            "srv", _persistent(control), tool, arguments, timeout=1
        )


async def test_death_with_child_holding_stdout_is_gone(
    tmp_path, mcp_reaper, mcp_owner
):
    """Конца вывода нет (его держит потомок), но лидер мёртв — смерть."""
    control = tmp_path / "s.json"
    set_behavior(control, child_keeps_stdout=True)

    with pytest.raises(McpServerGone):
        await call_tool("srv", _persistent(control), "crash", {}, timeout=1)


async def test_close_mid_call_is_gone(tmp_path, mcp_reaper, mcp_owner):
    """Сессию закрыли посреди вызова — смерть, даже пока процесс жив.

    Сервер глух к SIGTERM: закрытие добьёт его только через несколько
    секунд, и срок вызова истекает раньше, при живом процессе и открытом
    выводе. Решает то, что процесс снят с сессии.
    """
    control = tmp_path / "s.json"
    set_behavior(control, ignore_term=True)
    cfg = _persistent(control)
    registry = PersistentStdioRegistry.instance()
    call = asyncio.create_task(
        call_tool("srv", cfg, "nap", {"seconds": 30}, timeout=2)
    )
    await wait_for(lambda: _requests(control, "tools/call"))
    session = registry.get_or_create("srv", cfg)

    closing = asyncio.create_task(registry.aclose_one("srv"))
    await wait_for(lambda: session._proc is None, 1.0)

    with pytest.raises(McpServerGone):
        await call
    assert await asyncio.wait_for(closing, _CEILING_SEC)


async def test_one_shot_stdio_death_is_error(tmp_path, mcp_reaper, mcp_owner):
    control = tmp_path / "s.json"
    set_behavior(control)
    cfg = {**stdio_cfg(control), "persistent_session": False}

    with pytest.raises(McpServerGone, match="завершился, не ответив"):
        await _acall_with_retry(cfg, "crash")

    assert len(_requests(control, "tools/call")) == 1


async def test_one_shot_death_on_initialize_is_retried(
    tmp_path, mcp_reaper, mcp_owner, monkeypatch
):
    """Рукопожатие — не вызов: сбой на ``initialize`` повторяется."""
    monkeypatch.setattr(aretry, "_wait_before_retry", FakeClock().sleep)
    control = tmp_path / "s.json"
    set_behavior(control, exit_on="initialize")
    cfg = {**stdio_cfg(control), "persistent_session": False}

    with pytest.raises(ConnectionError, match="initialize") as caught:
        await _acall_with_retry(cfg, "echo")

    assert not isinstance(caught.value, McpCallOutcomeUnknown)
    assert len(launches(control)) > 1
    assert _requests(control, "tools/call") == []


@pytest.mark.parametrize(
    "persistent", [True, False], ids=["постоянный", "разовый"]
)
async def test_server_dying_after_initialize_fails_discovery(
    tmp_path, mcp_reaper, mcp_owner, persistent
):
    control = tmp_path / "s.json"
    set_behavior(control, exit_on="tools/list")
    cfg = {**stdio_cfg(control), "persistent_session": persistent}

    tools, status = await asyncio.wait_for(
        reg._adiscover_one("srv", cfg, timeout=5.0), _CEILING_SEC
    )

    assert _requests(control, "tools/list"), "список не запрашивался"
    assert tools == []
    assert status["status"] == "failed"
    assert status["reason"] == "сервер завершился, не ответив"


def test_close_before_the_request_is_not_running(monkeypatch):
    """Сессию закрыли между запуском и запросом: не ``AttributeError``."""
    session = PersistentStdioSession({"transport": "stdio"})
    monkeypatch.setattr(session, "_ensure_started", lambda timeout: None)

    with pytest.raises(RuntimeError, match=_NOT_RUNNING_MSG):
        session.call("tools/call", {}, 1.0)


def test_read_matching_on_closed_stream_still_empty():
    """Чтение по-прежнему отдаёт ``{}``: ошибку решает ``call``."""
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    os.close(write_fd)
    session = PersistentStdioSession({"transport": "stdio"})
    session._proc = SimpleNamespace(stdout=stream, stdin=None)
    try:
        assert session._read_matching(2, timeout=2) == {}
    finally:
        stream.close()
        forget_stdio_reader(stream)
