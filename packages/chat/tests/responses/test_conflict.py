"""Второй ход того же треда, пока первый не зафиксирован, — 409 без send."""

import asyncio
import json

import pytest
from gc_chat.responses.errors import ApiError
from gc_chat.responses.facade import ResponsesFacade
from gc_chat.responses.index_store import ResponseIndex
from gc_chat.responses.turn import TurnRun
from gc_testkit.settings import make_settings


class FakeAgent:
    def __init__(self, release: asyncio.Event) -> None:
        self.release = release
        self.calls: list[dict] = []
        self.sent: list[dict] = []
        self.stopped = 0
        self.started = asyncio.Event()
        self._hold = True
        self._answer = "готово"
        self.chunks: list[tuple[str, str]] | None = None
        self.executor = type("E", (), {})()
        self.executor.last_terminal_outcome = None
        self.executor._ctx = type("C", (), {})()

    async def stream(self, prompt, thread_id=None, system=None):
        self.calls.append(
            {"prompt": prompt, "thread_id": thread_id, "system": system}
        )
        self.started.set()
        chunks = [("answer", "кусок")] if self.chunks is None else self.chunks
        for kind, text in chunks:
            yield (kind, text)
        if self._hold:
            self._hold = False
            await self.release.wait()

    @property
    def last_answer(self):
        return self._answer

    def stop(self) -> None:
        self.stopped += 1
        self.release.set()

    def send(self, message: dict) -> None:
        self.sent.append(message)

    async def aclose(self) -> None:
        return None


def _facade(tmp_path, agent: FakeAgent) -> ResponsesFacade:
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


async def test_second_post_is_409_and_does_not_send(tmp_path):
    release = asyncio.Event()
    agent = FakeAgent(release)
    facade = _facade(tmp_path, agent)
    first = await facade.open({"model": "ui", "input": "первый"})
    await agent.started.wait()
    with pytest.raises(ApiError) as caught:
        await facade.open(
            {
                "model": "ui",
                "input": "второй",
                "previous_response_id": first.response_id,
            }
        )
    assert caught.value.status == 409
    assert caught.value.code == "conflict"
    assert agent.sent == []
    assert len(agent.calls) == 1
    assert agent.calls[0]["system"] == "responses"
    release.set()
    done = await first.wait_finished()
    assert done["status"] == "completed"
    assert agent.sent == []
    nxt = await facade.open(
        {
            "model": "ui",
            "input": "третий",
            "previous_response_id": first.response_id,
        }
    )
    continued = await nxt.wait_finished()
    assert continued["status"] == "completed"
    assert len(agent.calls) == 2
    assert agent.calls[1]["thread_id"] == agent.calls[0]["thread_id"]
    assert agent.calls[1]["system"] == "responses"
    assert agent.calls[1]["prompt"] == "третий"
    assert agent.sent == []


async def test_history_array_starts_only_the_last_user_text(tmp_path):
    agent = FakeAgent(asyncio.Event())
    agent._hold = False
    facade = _facade(tmp_path, agent)
    handle = await facade.open(
        {
            "model": "ui-model",
            "input": [
                {"role": "user", "content": "старое"},
                {
                    "type": "function_call_output",
                    "call_id": "c",
                    "output": "тело",
                },
                {"type": "input_image", "image_url": "http://img"},
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": "http://img"},
                        {"type": "input_text", "text": "только это"},
                    ],
                },
            ],
            "store": False,
        }
    )
    body = await handle.wait_finished()
    assert agent.calls[0]["prompt"] == "только это"
    assert agent.calls[0]["system"] == "responses"
    assert body["model"] == "ui-model"
    assert body["status"] == "completed"
    assert facade.get(handle.response_id)["id"] == handle.response_id


async def test_empty_last_element_does_not_start(tmp_path):
    agent = FakeAgent(asyncio.Event())
    facade = _facade(tmp_path, agent)
    with pytest.raises(ApiError) as caught:
        await facade.open(
            {
                "model": "m",
                "input": [
                    {"role": "user", "content": "было"},
                    {"type": "function_call_output", "output": "x"},
                ],
            }
        )
    assert caught.value.status == 400
    assert agent.calls == []
    assert agent.sent == []


async def test_cancel_then_second_cancel_does_not_stop_again(tmp_path):
    release = asyncio.Event()
    agent = FakeAgent(release)
    facade = _facade(tmp_path, agent)
    handle = await facade.open({"model": "m", "input": "стоп"})
    await agent.started.wait()
    cancelled = await facade.cancel(handle.response_id)
    assert cancelled["status"] == "cancelled"
    assert agent.stopped == 1
    again = await facade.cancel(handle.response_id)
    assert again["status"] == "cancelled"
    assert agent.stopped == 1
    with pytest.raises(ApiError) as missing:
        await facade.cancel("resp_00000000-0000-0000-0000-000000000000")
    assert missing.value.status == 404


async def test_get_survives_a_new_facade(tmp_path):
    agent = FakeAgent(asyncio.Event())
    agent._hold = False
    facade = _facade(tmp_path, agent)
    handle = await facade.open({"model": "m", "input": "сохрани"})
    saved = await handle.wait_finished()
    threads = tmp_path / "threads"
    restarted = ResponsesFacade(
        settings=make_settings(**{"agent.model": "m"}),
        agent_factory=facade._agent_factory,
        index=ResponseIndex(str(threads)),
    )
    assert restarted.get(handle.response_id) == saved


async def test_cwd_mismatch_does_not_start(tmp_path):
    agent = FakeAgent(asyncio.Event())
    agent._hold = False
    facade = _facade(tmp_path, agent)
    work = tmp_path / "work"
    work.mkdir()
    handle = await facade.open(
        {"model": "m", "input": "тут", "metadata": {"cwd": str(work)}}
    )
    await handle.wait_finished()
    with pytest.raises(ApiError) as caught:
        await facade.open(
            {
                "model": "m",
                "input": "там",
                "previous_response_id": handle.response_id,
                "metadata": {"cwd": "/tmp/other-root"},
            }
        )
    assert caught.value.status == 400
    assert len(agent.calls) == 1


def _finish_arguments(**extra) -> dict:
    payload = {
        "solution_type": "direct_answer",
        "task": "прочитать README",
        "task_report": "В README одна фраза.",
        "handoff": '{"artifacts":["README.md"]}',
    }
    payload.update(extra)
    return payload


def _output_text(body: dict) -> str:
    parts: list[str] = []
    for item in body["output"]:
        if item.get("type") != "message":
            continue
        for block in item.get("content") or []:
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


async def test_finish_arguments_become_report_not_raw_json(tmp_path):
    agent = FakeAgent(asyncio.Event())
    agent._hold = False
    agent.chunks = []
    agent._answer = json.dumps(
        _finish_arguments(
            comment="файл короткий",
            message="Finish gate: accept.",
            finish_gate={"action": "accept"},
        ),
        ensure_ascii=False,
    )
    facade = _facade(tmp_path, agent)
    handle = await facade.open({"model": "m", "input": "прочитай"})
    body = await handle.wait_finished()
    text = _output_text(body)
    assert text == "В README одна фраза.\n\n* заметка: файл короткий"
    assert "solution_type" not in text
    assert "Finish gate" not in text


async def test_shown_report_is_not_repeated_with_a_note(tmp_path):
    agent = FakeAgent(asyncio.Event())
    agent._hold = False
    agent.chunks = [("answer", "В README одна фраза.")]
    agent._answer = json.dumps(
        _finish_arguments(comment="файл короткий"),
        ensure_ascii=False,
    )
    facade = _facade(tmp_path, agent)
    handle = await facade.open({"model": "m", "input": "прочитай"})
    body = await handle.wait_finished()
    assert _output_text(body) == "В README одна фраза."


async def test_cancel_before_second_turn_skips_the_model(tmp_path):
    agent = FakeAgent(asyncio.Event())
    agent._hold = False
    facade = _facade(tmp_path, agent)
    first = await facade.open({"model": "m", "input": "первый"})
    await first.wait_finished()
    assert len(agent.calls) == 1

    entered = asyncio.Event()
    release = asyncio.Event()
    original = TurnRun._forward

    async def hold(self):
        entered.set()
        await release.wait()
        await original(self)

    TurnRun._forward = hold
    try:
        second = await facade.open(
            {
                "model": "m",
                "input": "второй",
                "previous_response_id": first.response_id,
            }
        )
        await entered.wait()
        cancel_task = asyncio.create_task(facade.cancel(second.response_id))
        while not second.cancel_requested:
            await asyncio.sleep(0)
        release.set()
        body = await cancel_task
    finally:
        TurnRun._forward = original

    assert body["status"] == "cancelled"
    assert len(agent.calls) == 1
