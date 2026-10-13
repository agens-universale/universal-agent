"""Общий стенд HTTP-фасада: фальшивый агент и клиент ASGI."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
from gc_a2a.facade import A2aFacade
from gc_a2a.http_api import create_app
from gc_testkit.settings import make_settings


class AnswerAgent:
    def __init__(self, text: str = "В src 42 файла.") -> None:
        self.text = text
        self.executor = type("E", (), {})()
        self.executor._ctx = type("C", (), {})()
        self.calls: list[dict] = []
        self.closed = False
        self.stopped = False
        self.launch_root = None
        self.last_answer = text

    async def stream(self, prompt, thread_id=None, system=None):
        self.calls.append(
            {"prompt": prompt, "thread_id": thread_id, "system": system}
        )
        self.last_answer = self.text
        if False:
            yield ("answer", self.text)

    def stop(self) -> None:
        self.stopped = True

    async def aclose(self) -> None:
        self.closed = True


class AskAgent(AnswerAgent):
    def __init__(self, question: str = "Какие файлы?") -> None:
        super().__init__()
        self.question = question

    async def stream(self, prompt, thread_id=None, system=None):
        self.calls.append(
            {"prompt": prompt, "thread_id": thread_id, "system": system}
        )
        answer = await self.executor._ctx.ask_human(self.question)
        self.last_answer = "учтено: " + answer
        if False:
            yield ("answer", "")


class SlowAgent(AnswerAgent):
    async def stream(self, prompt, thread_id=None, system=None):
        self.calls.append(
            {"prompt": prompt, "thread_id": thread_id, "system": system}
        )
        while not self.stopped:
            await asyncio.sleep(0.01)
        if False:
            yield ("answer", "")


class FailAgent(AnswerAgent):
    async def stream(self, prompt, thread_id=None, system=None):
        del prompt, thread_id, system
        raise RuntimeError("boom")
        yield ("answer", "")


class Clock:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"2026-10-12T15:04:05.{self.n:03d}Z"


@asynccontextmanager
async def session(tmp_path, *, token="", agents=None, clock=None, **settings):
    created: list[AnswerAgent] = []
    queue = list(agents or [])

    async def factory(agent_settings, **kwargs):
        del kwargs
        agent = queue.pop(0) if queue else AnswerAgent()
        agent.launch_root = agent_settings.runtime.launch_root
        created.append(agent)
        return agent

    overrides = {"paths.threads_data_path": str(tmp_path), **settings}
    if token:
        overrides["a2a.auth_token"] = token
    facade = A2aFacade(
        settings=make_settings(overrides),
        agent_factory=factory,
        clock=clock,
    )
    transport = httpx.ASGITransport(app=create_app(facade))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8091"
    ) as client:
        try:
            yield client, facade, created
        finally:
            await facade.shutdown()


def message(text="Сколько файлов в src?", message_id="m-1", **fields):
    body = {
        "messageId": message_id,
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    body.update(fields)
    return {"message": body}


async def rpc(
    client,
    method,
    params=None,
    *,
    version="1.0",
    token=None,
    query=None,
    extra_headers=None,
):
    headers = dict(extra_headers or {})
    if version is not None:
        headers["A2A-Version"] = version
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    url = "/rpc" if query is None else "/rpc?" + query
    return await client.post(
        url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {} if params is None else params,
        },
        headers=headers,
    )
