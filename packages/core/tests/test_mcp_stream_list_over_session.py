"""Список тулов потокового сервера по постоянной сессии потребителя.

С ключом сессии в определении и ``persistent_session`` список тулов идёт
по той же сессии, что потом обслужит вызовы: процессный сервер стартует
один раз. Сбой такого списка — отказ без разового соединения. Без ключа
список, как прежде, берётся отдельным разовым соединением.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from gc_core.tools.connect_mcp.durable_scope import (
    McpSessionLostError,
    durable_mcp_calls,
)
from gc_core.tools.connect_mcp.registry import alist_raw_tools
from gc_core.tools.connect_mcp.server_identity import SESSION_KEY_FIELD
from gc_testkit.mcp import (
    alive,
    call_tool,
    events,
    launches,
    set_behavior,
    stdio_cfg,
    stream_server,
    wait_for,
)
from gc_testkit.mcp.fixtures import mcp_owner, mcp_reaper
from mcp.shared.exceptions import McpError

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="процессы и сигналы POSIX"
)

#: Потолок одного ожидания в тесте: зависание не повесит прогон.
_CEILING_SEC = 30.0
_LIST_SEC = 10.0
_STREAMS = pytest.mark.parametrize("transport", ["stream_http", "stream_sse"])


def _persistent(cfg: dict, key: str | None = None) -> dict:
    keyed = {SESSION_KEY_FIELD: key} if key else {}
    return {**cfg, "persistent_session": True, **keyed}


def _stdio(control, key: str | None = None) -> dict:
    return _persistent(
        {**stdio_cfg(control), "transport": "stream_stdio"}, key
    )


async def _list(cfg: dict, timeout: float = _LIST_SEC) -> list[str]:
    tools = await asyncio.wait_for(
        alist_raw_tools("srv", cfg, timeout=timeout), _CEILING_SEC
    )
    return [tool["name"] for tool in tools]


def _first_list_session(control) -> int:
    """Номер сессии потокового сервера, по которой шёл первый ``tools/list``.

    Первый — список теста: вызов SDK сам просит список у сессии, которой
    схемы тулов ещё не известны.
    """
    return next(
        int(line.split()[3])
        for line in events(control)
        if line.startswith("req tools/list ")
    )


async def test_stream_stdio_list_then_call_is_one_launch(
    tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    cfg = _stdio(control, "srv@1")

    names = await _list(cfg)
    out = await call_tool("srv", cfg, "echo", {"text": "x"})

    assert names == ["echo", "nap"]
    assert launches(control) == [out["pid"]]


async def test_list_failure_has_no_one_shot_fallback(
    tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    set_behavior(control, exit_on="tools/list")
    cfg = _stdio(control, "srv@1")

    with pytest.raises(McpError):
        await _list(cfg)
    assert len(launches(control)) == 1

    # Список шёл по постоянной сессии: её сбой под durable — «исход
    # неизвестен», как у вызова; разовый список так не отвечает.
    with durable_mcp_calls(), pytest.raises(McpSessionLostError):
        await _list(cfg)
    assert len(launches(control)) == 2


@_STREAMS
async def test_stream_list_and_call_share_session(
    transport, tmp_path, mcp_reaper, mcp_owner
):
    async with stream_server(tmp_path, transport) as (base, control):
        cfg = _persistent(base, "st@1")
        names = await _list(cfg)
        out = await call_tool("st", cfg, "action", {})

    assert names == ["action"]
    assert _first_list_session(control) == out["session"]


@_STREAMS
async def test_without_key_list_is_one_shot(
    transport, tmp_path, mcp_reaper, mcp_owner
):
    async with stream_server(tmp_path, transport) as (base, control):
        cfg = _persistent(base)
        await _list(cfg)
        out = await call_tool("st", cfg, "action", {})

    assert _first_list_session(control) != out["session"]


async def test_stream_stdio_without_key_list_is_one_shot(
    tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    cfg = _stdio(control)

    await _list(cfg)
    out = await call_tool("srv", cfg, "echo", {"text": "x"})

    assert len(launches(control)) == 2
    assert launches(control)[-1] == out["pid"]


async def test_list_over_session_is_bounded_by_timeout(
    tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    set_behavior(control, start_delay=60)
    cfg = _stdio(control, "srv@1")
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        await _list(cfg, timeout=0.5)

    # Срок — на открытие и список; закрытие сессии гасит процесс.
    assert time.monotonic() - started < 15
    [pid] = launches(control)
    await wait_for(lambda: not alive(pid))
