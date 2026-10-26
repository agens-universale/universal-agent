"""Потолок снятия не теряется: отмена отвечает ошибкой, фон пишет в лог."""

from __future__ import annotations

import asyncio
import gc
import json
import logging
from types import SimpleNamespace

import pytest
from gc_chat.completions.turn import TurnRun as CompletionsTurn
from gc_chat.responses.http_api import cancel_response, create_app
from gc_chat.responses.facade import ResponsesFacade
from gc_chat.responses.index_store import ResponseIndex
from gc_core.user_stop import UserStop
from gc_testkit.settings import make_settings
from starlette.requests import Request


class _HoldAgent:
    """Стоп бьёт в gate, release не заканчивается, пока не отпустят."""

    def __init__(self, gate: UserStop) -> None:
        self.gate = gate
        self.done = asyncio.Event()
        self.started = asyncio.Event()
        self.stopped = 0
        self.executor = SimpleNamespace(
            last_terminal_outcome=None,
            _ctx=SimpleNamespace(),
            user_stop=gate,
        )
        self._executor = self.executor

    async def stream(self, prompt, thread_id=None, system=None):
        del prompt, thread_id, system

        async def release() -> None:
            await self.done.wait()

        async with self.gate.arm(kind="exec", release=release):
            self.started.set()
            yield ("answer", "кусок")
            await self.done.wait()

    @property
    def last_answer(self) -> str:
        return "кусок"

    def stop(self) -> None:
        self.stopped += 1
        self.gate.request()

    def send(self, message: dict) -> None:
        del message

    async def aclose(self) -> None:
        return None


def _facade(tmp_path, agent: _HoldAgent) -> ResponsesFacade:
    threads = tmp_path / "threads"
    threads.mkdir()

    async def factory(settings, **kwargs):
        del settings, kwargs
        return agent

    return ResponsesFacade(
        settings=make_settings(
            **{"agent.model": "from-config", "chat_api.launch_root": None}
        ),
        agent_factory=factory,
        index=ResponseIndex(str(threads)),
    )


def _request(app) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 123),
            "server": ("127.0.0.1", 80),
            "app": app,
        }
    )


def _logged_timeout(caplog, logger_name: str) -> bool:
    return any(
        rec.name == logger_name
        and rec.exc_info is not None
        and isinstance(rec.exc_info[1], TimeoutError)
        for rec in caplog.records
    )


async def _wait_logged(caplog, logger_name: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 1
    while loop.time() < deadline:
        if _logged_timeout(caplog, logger_name):
            return
        await asyncio.sleep(0.01)
    gc.collect()
    await asyncio.sleep(0)


async def test_cancel_settle_timeout_is_an_api_error(tmp_path, monkeypatch):
    """Отмена не отдаёт тело успеха, если снятие не уложилось в потолок."""
    monkeypatch.setattr("gc_core.user_stop._SETTLE_SEC", 0.05)
    agent = _HoldAgent(UserStop())
    facade = _facade(tmp_path, agent)
    handle = await facade.open({"model": "m", "input": "стоп"})
    try:
        await agent.started.wait()
        try:
            response = await cancel_response(
                handle.response_id, _request(create_app(facade))
            )
        except TimeoutError:
            pytest.fail("TimeoutError вышел из отмены необработанным")
        assert response.status_code == 500
        assert response.media_type == "application/json"
        body = json.loads(response.body)
        assert body["error"]["code"] == "server_error"
        assert body["error"]["message"] == "user stop did not settle"
        assert "status" not in body
    finally:
        agent.done.set()
        await handle.done.wait()


async def test_completions_background_stop_timeout_is_logged(
    caplog, monkeypatch
):
    """Отвал стрима не бросает TimeoutError мимо задачи."""
    monkeypatch.setattr("gc_core.user_stop._SETTLE_SEC", 0.05)
    agent = _HoldAgent(UserStop())
    run = CompletionsTurn(
        agent=agent,
        content=None,
        flow=None,
        prompt="",
        thread_id="t",
        threads_dir="",
        completion_id="c",
        created=0,
        model="m",
    )
    gen = agent.stream("")
    await gen.__anext__()
    caplog.set_level(logging.ERROR)
    try:
        run.stop_if_open()
        await _wait_logged(caplog, "gc_chat.completions.turn")
        gc.collect()
        await asyncio.sleep(0)
        assert _logged_timeout(caplog, "gc_chat.completions.turn")
        assert not any(
            "was never retrieved" in rec.getMessage() for rec in caplog.records
        )
    finally:
        agent.done.set()
        await gen.aclose()


async def test_responses_background_stop_timeout_is_logged(
    tmp_path, caplog, monkeypatch
):
    """Фоновый стоп responses забирает свой TimeoutError."""
    monkeypatch.setattr("gc_core.user_stop._SETTLE_SEC", 0.05)
    agent = _HoldAgent(UserStop())
    facade = _facade(tmp_path, agent)
    handle = await facade.open({"model": "m", "input": "стоп"})
    caplog.set_level(logging.ERROR)
    try:
        await agent.started.wait()
        handle._stop_if_open()
        await _wait_logged(caplog, "gc_chat.responses.turn")
        gc.collect()
        await asyncio.sleep(0)
        assert _logged_timeout(caplog, "gc_chat.responses.turn")
        assert not any(
            "was never retrieved" in rec.getMessage() for rec in caplog.records
        )
    finally:
        agent.done.set()
        await handle.done.wait()
