"""Ход агента, у которого всё в базе: треды и ресурсы — в SQLite, ресурсы
залиты из пакета. Модель видит то же, что с файлами: системный промпт и
набор тулов совпадают байт в байт."""

import json

import httpx
import pytest
from gc_core.agent_store import FileAgentStore, Prompt
from gc_core.aio import AsyncAgent
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_stores import sqlite
from gc_stores.defaults import bundled_source
from gc_testkit.settings import make_settings

FINISH = {
    "id": "scripted-1",
    "object": "chat.completion",
    "model": "scripted",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "finish",
                        "type": "function",
                        "function": {
                            "name": "turn_finish",
                            "arguments": json.dumps(
                                {
                                    "solution_type": "full_solution",
                                    "task": "demo",
                                    "task_report": "готово",
                                    "handoff": "{}",
                                }
                            ),
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def _as_sse(turn: dict) -> str:
    """Тот же ход стримом: вызовы тулов, затем finish_reason и usage."""
    message = turn["choices"][0]["message"]
    calls = [
        {"index": i, **call} for i, call in enumerate(message["tool_calls"])
    ]
    chunks = [
        {
            "id": turn["id"],
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "tool_calls": calls},
                }
            ],
        },
        {
            "id": turn["id"],
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
            ],
            "usage": turn["usage"],
        },
    ]
    body = "".join(
        f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks
    )
    return body + "data: [DONE]\n\n"


@pytest.fixture
def requests_seen(monkeypatch):
    from gc_core.utils import connect_backend

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        seen.append(payload)
        if payload.get("stream"):
            return httpx.Response(
                200,
                text=_as_sse(FINISH),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=FINISH)

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_kw: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    return seen


def _settings():
    return make_settings(
        {
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://scripted.invalid/v1/chat/completions",
            "backend.chat_completion_kwargs": {"stream": False},
            "agent.model": "scripted",
            "agent.max_steps": 3,
            "agent.use_local_subagents": False,
            "agent.strict_turn_finish_validation": False,
            "logging.save_logs": False,
            "logging.console_trace": False,
        }
    )


async def _copy_defaults(writer, settings) -> None:
    """Промпты и системные скиллы пакета — в хранилище (как
    ``gc-stores sync --with-defaults``)."""
    source = FileAgentStore(settings)
    for name in Prompt:
        try:
            data = await source.read("prompt", name)
        except OSError:
            data = None
        if data is not None:
            await writer.put_resource(
                "prompt", name, data, source=bundled_source()
            )
    pending = ["system"]
    while pending:
        prefix = pending.pop()
        for entry in await source.list("skill", prefix):
            path = f"{prefix}/{entry.name}"
            if entry.type == "dir":
                pending.append(path)
            else:
                data = await source.read("skill", path)
                await writer.put_resource(
                    "skill", path, data, source=bundled_source()
                )


async def _first_request(settings, requests_seen, **stores):
    requests_seen.clear()
    async with await AsyncAgent.create(settings, **stores) as agent:
        await agent.run("Сделай демо", thread_id="demo", system="cli")
    return requests_seen[0]


async def test_agent_sees_the_same_prompt_and_tools(
    tmp_path, monkeypatch, requests_seen
):
    monkeypatch.chdir(tmp_path)
    settings = _settings()
    on_files = await _first_request(
        settings,
        requests_seen,
        thread_store=FileThreadStore(str(tmp_path / "threads")),
    )
    stores = sqlite.open_stores(tmp_path / "agent.db", agent_id="a")
    await _copy_defaults(stores.agent, settings)
    try:
        in_database = await _first_request(
            settings, requests_seen, stores=stores
        )
    finally:
        await stores.aclose()
    assert in_database["messages"][0] == on_files["messages"][0]
    assert in_database["tools"] == on_files["tools"]


async def test_system_prompt_comes_from_the_database(
    tmp_path, monkeypatch, requests_seen
):
    monkeypatch.chdir(tmp_path)
    settings = _settings()
    stores = sqlite.open_stores(tmp_path / "agent.db", agent_id="a")
    await _copy_defaults(stores.agent, settings)
    await stores.agent.put_resource(
        "prompt", Prompt.SYSTEM, "Свой системный промпт из базы.".encode()
    )
    try:
        request = await _first_request(settings, requests_seen, stores=stores)
    finally:
        await stores.aclose()
    assert (
        "Свой системный промпт из базы." in request["messages"][0]["content"]
    )
    threads = sqlite.SQLiteThreadStore(tmp_path / "agent.db")
    record = await threads.get_thread(ThreadKey(system="cli", thread="demo"))
    assert record is not None and record.info.messages_count, "тред — в базе"
