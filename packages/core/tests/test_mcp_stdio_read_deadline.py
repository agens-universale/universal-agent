"""Ответ постоянного stdio ждётся по общему сроку, а не по числу строк.

Уведомления и строки не-JSON пропускаются без ограничения числа: болтливый
сервер не теряет ответ. Срок общий от начала чтения, на каждую строку идёт
остаток — поток уведомлений не растягивает ожидание. Конец потока и истёкший
срок дают ``{}``.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from types import SimpleNamespace

import pytest
from gc_core.tools.connect_mcp import persistent_stdio as ps
from gc_core.tools.connect_mcp.transport import forget_stdio_reader
from gc_testkit.mcp import FakeClock, call_tool, set_behavior, stdio_cfg
from gc_testkit.mcp.fixtures import mcp_owner, mcp_reaper

# Потолок одного чтения в тестах с двойником: само чтение не ждёт стенных
# секунд, потолок только не даёт прогону повиснуть.
_READ_CEILING_SEC = 5.0

#: Число строк двойника до конца потока.
_LINES_BEFORE_EOF = 40

_NOTIFICATION = (
    json.dumps({"jsonrpc": "2.0", "method": "notifications/message"}) + "\n"
)


def _persistent(control) -> dict:
    return {**stdio_cfg(control), "persistent_session": True}


def _session(proc) -> ps.PersistentStdioSession:
    session = ps.PersistentStdioSession({"transport": "stdio"})
    session._proc = proc
    return session


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, _READ_CEILING_SEC))


#: Три чтения ответа: все обязаны вести себя одинаково.
_READS = {
    "_read_matching": lambda s, p, t: s._read_matching(7, t),
    "_read_matching_on": lambda s, p, t: s._read_matching_on(p, 7, t),
    "_aread_matching_on": lambda s, p, t: _run(s._aread_matching_on(p, 7, t)),
}


class _ChattyReader:
    """Двойник чтения строки: уведомление каждые ``step`` с подставных часов.

    Как настоящий читатель, ждёт строку не дольше переданного срока: срок
    меньше шага — истёк, строки нет. После 40 строк — конец потока.
    """

    def __init__(self, clock: FakeClock, step: float) -> None:
        self._clock = clock
        self._step = step
        self.timeouts: list[float] = []

    def readline(self, stream, timeout: float) -> str | None:
        self.timeouts.append(timeout)
        if timeout < self._step:
            self._clock.advance(timeout)
            return None
        if len(self.timeouts) > _LINES_BEFORE_EOF:
            return None
        self._clock.advance(self._step)
        return _NOTIFICATION

    async def areadline(self, stream, timeout: float) -> str | None:
        return self.readline(stream, timeout)


@pytest.mark.skipif(os.name != "posix", reason="процессы POSIX")
async def test_response_after_forty_notifications(
    tmp_path, mcp_reaper, mcp_owner
):
    control = tmp_path / "s.json"
    set_behavior(control, notifications=40)

    out = await call_tool("srv", _persistent(control), "echo", {"text": "hi"})

    assert out["text"] == "hi"


@pytest.mark.skipif(os.name != "posix", reason="процессы POSIX")
async def test_non_json_lines_are_skipped(tmp_path, mcp_reaper, mcp_owner):
    control = tmp_path / "s.json"
    set_behavior(control, noise=True)

    out = await call_tool("srv", _persistent(control), "echo", {"text": "hi"})

    assert out["text"] == "hi"


def _chatty(monkeypatch, step: float) -> _ChattyReader:
    """Подставить часы и двойник чтения строки в модуль сессии."""
    clock = FakeClock()
    reader = _ChattyReader(clock, step)
    monkeypatch.setattr(ps, "_clock", clock.now)
    monkeypatch.setattr(ps, "_readline_with_timeout", reader.readline)
    monkeypatch.setattr(ps, "_areadline_with_timeout", reader.areadline)
    return reader


@pytest.mark.parametrize("read", sorted(_READS))
def test_deadline_is_total_not_per_line(monkeypatch, read):
    reader = _chatty(monkeypatch, step=0.2)
    proc = SimpleNamespace(stdout=object())

    got = _READS[read](_session(proc), proc, 1.0)

    assert got == {}
    assert len(reader.timeouts) == 5
    assert reader.timeouts == pytest.approx([1.0, 0.8, 0.6, 0.4, 0.2])


@pytest.mark.parametrize("read", sorted(_READS))
def test_lines_arriving_until_the_deadline_stop_on_it(monkeypatch, read):
    """Строки приходят ровно в срок: чтение кончает сам срок, не читатель.

    Шаг 0,25 точен в двоичной записи: каждая из четырёх строк приходит,
    после четвёртой остаток ровно ноль, и пятого чтения нет.
    """
    reader = _chatty(monkeypatch, step=0.25)
    proc = SimpleNamespace(stdout=object())

    got = _READS[read](_session(proc), proc, 1.0)

    assert got == {}
    assert reader.timeouts == [1.0, 0.75, 0.5, 0.25]
    assert all(t > 0 for t in reader.timeouts)


@pytest.mark.parametrize("read", sorted(_READS))
def test_closed_stream_still_gives_empty(read):
    stream = io.BytesIO(b"")
    proc = SimpleNamespace(stdout=stream)
    try:
        got = _READS[read](_session(proc), proc, _READ_CEILING_SEC)
    finally:
        forget_stdio_reader(stream)

    assert got == {}
