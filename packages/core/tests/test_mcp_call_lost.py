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
    McpCallTimedOut,
    McpServerGone,
)
from gc_core.tools.connect_mcp.durable_scope import (
    McpSessionLostError,
    durable_mcp_calls,
)
from gc_core.tools.connect_mcp.persistent_stdio import PersistentStdioSession
from gc_core.tools.connect_mcp.transport import forget_stdio_reader
from gc_testkit.mcp import (
    call_tool,
    events,
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


async def test_durable_gets_outcome_unknown(tmp_path, mcp_reaper, mcp_owner):
    control = tmp_path / "s.json"
    set_behavior(control)

    with durable_mcp_calls(), pytest.raises(McpSessionLostError):
        await call_tool(
            "srv", _persistent(control), "nap", {"seconds": 30}, timeout=1
        )


async def test_one_shot_stdio_death_is_error(tmp_path, mcp_reaper, mcp_owner):
    control = tmp_path / "s.json"
    set_behavior(control)
    cfg = {**stdio_cfg(control), "persistent_session": False}

    with pytest.raises(McpServerGone, match="завершился, не ответив"):
        await _acall_with_retry(cfg, "crash")

    assert len(_requests(control, "tools/call")) == 1


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
