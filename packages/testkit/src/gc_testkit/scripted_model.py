"""Скриптованная модель и помощники сквозных тестов хранилищ: ход
завершается ``turn_finish``, запросы к модели копятся для сравнения."""

import json

import httpx

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


def as_sse(turn: dict) -> str:
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


def install(monkeypatch) -> list[dict]:
    """Подменить транспорт к модели; вернуть список запросов к ней."""
    from gc_core.utils import connect_backend

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        seen.append(payload)
        if payload.get("stream"):
            return httpx.Response(
                200,
                text=as_sse(FINISH),
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


def scripted_settings():
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
