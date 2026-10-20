"""Входящий поток треда (спека сервера, 13.2): сообщение в идущий ход с
любого процесса.

Отправитель пишет в поток ``inbox`` треда без аренды (``Threads.submit``).
Исполнитель забирает хвост в начале хода и опросом во время хода, отметка
доставленного (``inbox_seq``) пишется той же записью, что и сообщения
истории — доставка ровно один раз. Пришедшее после последнего забора, пока
ход отпускает тред, доигрывается (рукопожатие).
"""

import asyncio
import json

import httpx
import pytest
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_testkit import scripted_model
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="srv", thread="t1")

THINK = {
    "id": "scripted-think",
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
                        "id": "think-1",
                        "type": "function",
                        "function": {
                            "name": "session_think",
                            "arguments": json.dumps({"thoughts": "думаю"}),
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}


def _settings(tmp_path):
    return make_settings(
        {
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://scripted.invalid/v1/chat/completions",
            "backend.chat_completion_kwargs": {"stream": False},
            "agent.model": "scripted",
            "agent.max_steps": 5,
            "agent.use_local_subagents": False,
            "agent.strict_turn_finish_validation": False,
            "logging.save_logs": False,
            "logging.console_trace": False,
            "paths.threads_data_path": str(tmp_path),
            "thread_store.inbox_poll_seconds": 0.01,
        }
    )


def _install(monkeypatch, plan):
    """Модель по плану: ``plan(index, payload)`` → ответ (корутина)."""
    from gc_core.utils import connect_backend

    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        seen.append(payload)
        return httpx.Response(200, json=await plan(len(seen) - 1, payload))

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_kw: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    return seen


async def _executor(tmp_path):
    from gc_core.agent_executor import AgentExecutor
    from gc_core.run_context import acreate_run_context

    ctx = await acreate_run_context(_settings(tmp_path), auth_connector=None)
    return AgentExecutor(ctx)


def _texts(payload) -> list[str]:
    return [
        str(m.get("content"))
        for m in payload.get("messages", [])
        if m.get("role") == "user"
    ]


def _mentions(payload, word: str) -> int:
    return sum(word in text for text in _texts(payload))


async def _history_count(tmp_path, word: str) -> int:
    entries = await Threads(FileThreadStore(tmp_path)).history(KEY)
    return sum(
        word in str(e.data.get("content"))
        for e in entries
        if e.data.get("role") == "user"
    )


async def _inbox_seq(tmp_path) -> int:
    record = await FileThreadStore(tmp_path).get_thread(KEY)
    return record.state.inbox_seq


async def _wait_queued(executor, word: str) -> None:
    for _ in range(500):
        with executor._lock:
            queued = [*executor._pending_messages]
        if any(word in str(m.get("content")) for m in queued):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"опрос не положил {word!r} в очередь хода")


async def test_submit_returns_the_inbox_seq(tmp_path):
    threads = Threads(FileThreadStore(tmp_path))
    first = await threads.submit(KEY, {"role": "user", "content": "а"})
    second = await threads.submit(KEY, {"role": "user", "content": "б"})
    assert (first.max_seq["inbox"], second.max_seq["inbox"]) == (1, 2)
    entries = await threads.store.read_stream(KEY, "inbox")
    assert entries[0].data["source"] == "api"
    assert entries[0].data["content"] == "а"


async def test_message_submitted_during_the_turn_reaches_the_next_step(
    monkeypatch, tmp_path
):
    holder: dict = {}

    async def plan(index, _payload):
        if index == 0:
            await Threads(FileThreadStore(tmp_path)).submit(
                KEY, {"role": "user", "content": "ещё и тесты"}
            )
            await _wait_queued(holder["ex"], "ещё и тесты")
            return THINK
        return scripted_model.FINISH

    seen = _install(monkeypatch, plan)
    holder["ex"] = ex = await _executor(tmp_path)
    await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)

    assert _mentions(seen[1], "ещё и тесты") == 1
    assert await _inbox_seq(tmp_path) == 1
    assert await _history_count(tmp_path, "ещё и тесты") == 1

    # Следующий ход его не повторяет.
    await ex.arun(["дальше"], thread_id=KEY.thread, system=KEY.system)
    assert _mentions(seen[-1], "ещё и тесты") == 1
    assert await _history_count(tmp_path, "ещё и тесты") == 1


async def test_tail_of_the_inbox_starts_the_run(monkeypatch, tmp_path):
    seen = _install(monkeypatch, _finish)
    threads = Threads(FileThreadStore(tmp_path))
    await threads.submit(KEY, {"role": "user", "content": "из очереди"})
    ex = await _executor(tmp_path)
    await ex.arun([], thread_id=KEY.thread, system=KEY.system)
    assert _mentions(seen[0], "из очереди") == 1
    assert await _inbox_seq(tmp_path) == 1


async def test_run_without_messages_and_empty_inbox_is_refused(tmp_path):
    ex = await _executor(tmp_path)
    with pytest.raises(ValueError):
        await ex.arun([], thread_id=KEY.thread, system=KEY.system)


async def test_message_after_the_last_take_is_played_by_the_handshake(
    monkeypatch, tmp_path
):
    """Сообщение легло, когда ход уже всё забрал и отпускает тред: исполнитель
    ещё раз смотрит поток и доигрывает его отдельным ходом."""
    import gc_core.agent_executor as ae

    real_release = ae._arelease_lease
    sent: list = []

    async def release_after_submit(thread_session, logger):
        if not sent:
            sent.append(
                await Threads(FileThreadStore(tmp_path)).submit(
                    KEY, {"role": "user", "content": "поздний"}
                )
            )
        await real_release(thread_session, logger)

    monkeypatch.setattr(ae, "_arelease_lease", release_after_submit)
    seen = _install(monkeypatch, _finish)
    ex = await _executor(tmp_path)
    await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)

    assert len(seen) == 2
    assert _mentions(seen[1], "поздний") == 1
    assert await _inbox_seq(tmp_path) == sent[0].max_seq["inbox"]


async def test_crash_before_the_step_write_redelivers_exactly_once(
    monkeypatch, tmp_path
):
    from gc_core.thread_store import ThreadSession

    seen = _install(monkeypatch, _finish)
    await Threads(FileThreadStore(tmp_path)).submit(
        KEY, {"role": "user", "content": "важное"}
    )
    real_persist = ThreadSession.apersist_batch
    calls: list = []

    async def dying_persist(self, *args, **kwargs):
        if not calls:
            calls.append(1)
            # Процесс умер посреди шага: не Exception, цикл её не глотает.
            raise _ProcessDied
        return await real_persist(self, *args, **kwargs)

    monkeypatch.setattr(ThreadSession, "apersist_batch", dying_persist)
    ex = await _executor(tmp_path)
    with pytest.raises(_ProcessDied):
        await ex.arun(["сделай"], thread_id=KEY.thread, system=KEY.system)
    assert await _history_count(tmp_path, "важное") == 0

    await ex.arun(["снова"], thread_id=KEY.thread, system=KEY.system)
    assert _mentions(seen[-1], "важное") == 1
    assert await _history_count(tmp_path, "важное") == 1
    assert await _inbox_seq(tmp_path) == 1


class _ProcessDied(BaseException):
    """Имитация гибели процесса: ход обрывается без уборки цикла."""


async def _finish(_index, _payload):
    return scripted_model.FINISH
