"""HTTP: стрим, вырезание think при посеве, удаление треда."""

import json

from fastapi.testclient import TestClient
from gc_chat.completions.facade import CompletionsFacade
from gc_chat.completions.history import SYSTEM
from gc_chat.completions.http_api import create_app
from gc_testkit.settings import make_settings

# Host-guard пускает только loopback (gc_chat.http_guard).
LOOPBACK = "http://127.0.0.1"


class _Agent:
    def __init__(self, threads: str) -> None:
        self.executor = type("E", (), {})()
        self.executor.last_terminal_outcome = None
        self.executor._ctx = type("C", (), {})()
        self.threads = threads
        self.calls: list[dict] = []
        self.seeded: list[str] = []
        self.last = "итог"

    async def stream(self, prompt, thread_id=None, system=None):
        self.calls.append(
            {"prompt": prompt, "thread_id": thread_id, "system": system}
        )
        self.seeded.append(_seed_text(self.threads, thread_id))
        yield ("think", "карточка")
        yield ("answer", "черновик")
        yield ("reset", "")
        yield ("answer", self.last)

    @property
    def last_answer(self):
        return self.last

    def stop(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _seed_text(threads: str, thread_id: str | None) -> str:
    if thread_id is None:
        return ""
    from pathlib import Path

    path = Path(threads) / f"data_{SYSTEM}_{thread_id}.jsonl"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _app(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()
    agent = _Agent(str(threads))

    async def factory(settings, **kwargs):
        del settings, kwargs
        return agent

    facade = CompletionsFacade(
        settings=make_settings(
            **{"agent.model": "agent-model"},
            **{"paths.threads_data_path": str(threads)},
        ),
        agent_factory=factory,
    )
    return create_app(facade), agent, threads


def _chunks(body: str) -> list[dict]:
    found = []
    for block in body.split("\n\n"):
        if "data: " not in block:
            continue
        data = block.split("data: ", 1)[1].strip()
        if data == "[DONE]":
            found.append({"done": True})
            continue
        found.append(json.loads(data))
    return found


def _content(chunks: list[dict]) -> str:
    parts = []
    for chunk in chunks:
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        text = delta.get("content")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def test_stream_wraps_trace_and_matches_non_stream(tmp_path):
    app, _agent, threads = _app(tmp_path)
    client = TestClient(app, base_url=LOOPBACK)
    body = {
        "model": "GC-UA",
        "messages": [
            {"role": "user", "content": "вопрос"},
            {
                "role": "assistant",
                "content": "<think>старый трейс</think>\nпрошлый ответ",
            },
            {"role": "user", "content": "дальше"},
        ],
        "stream": True,
    }
    response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200
    chunks = _chunks(response.text)
    text = _content(chunks)
    assert text == "<think>\nкарточка\nчерновик\n</think>\nитог"
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert chunks[-1] == {"done": True}
    assert _agent.calls[0]["prompt"] == "дальше"
    assert _agent.calls[0]["system"] == SYSTEM
    assert "старый трейс" not in _agent.seeded[0]
    assert "прошлый ответ" in _agent.seeded[0]
    assert list(threads.glob("data_*.jsonl")) == []

    plain = client.post(
        "/v1/chat/completions",
        json={
            "model": "GC-UA",
            "messages": [{"role": "user", "content": "ещё"}],
        },
    )
    assert plain.status_code == 200
    payload = plain.json()
    assert payload["model"] == "GC-UA"
    assert payload["choices"][0]["message"]["content"] == text
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"] is None
    assert _agent.calls[1]["thread_id"] != _agent.calls[0]["thread_id"]
    assert list(threads.glob("data_*.jsonl")) == []


def test_models_and_missing_user(tmp_path):
    app, _agent, _threads = _app(tmp_path)
    client = TestClient(app, base_url=LOOPBACK)
    models = client.get("/v1/models")
    assert models.status_code == 200
    ids = [item["id"] for item in models.json()["data"]]
    assert ids == ["GC-UA", "agent-model"]
    assert models.json()["data"][0]["owned_by"] == "gc-chat"
    rejected = client.post(
        "/v1/chat/completions",
        json={
            "model": "GC-UA",
            "messages": [{"role": "assistant", "content": "x"}],
        },
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "invalid_request"
