"""Сообщение, пришедшее прогону на излёте, доставляется, а не пропадает.

Сообщения задачи лежат во входящем потоке треда, а не в очереди
исполнителя. Прогон после цикла ещё сохраняет тред и сносит MCP-сессии —
через ``await``, и всё это время вебхук отвечает ``queued``. Такое сообщение
доигрывает рукопожатие исполнителя или проверка реестра, снявшего прогон;
остановленный и упавший прогон оставляют его в потоке следующему.

Исполнитель настоящий — ``AgentExecutor`` с его ``arun`` и опросом потока;
подменены только цикл агента (записывает батчи) и снос MCP (двойник выполняет
шаг теста ровно в окне сноса).
"""

import asyncio
import importlib
import types

import pytest
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_server.webhook.registry import AgentRunRegistry
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="s", thread="t")
#: Потолок любого ожидания: зависший прогон — красный тест, а не вечный набор.
_BOUND_SEC = 5.0


def _cfg(tmp_path):
    return make_settings(
        {
            "logging.save_logs": False,
            "paths.threads_data_path": str(tmp_path),
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://test",
            "agent.model": "x",
            "agent.max_steps": 1,
            "agent.system_prompt_id": "v1",
        }
    )


@pytest.fixture()
async def env(monkeypatch, tmp_path):
    from gc_core.run_context import acreate_run_context

    ae = importlib.import_module("gc_core.agent_executor")
    monkeypatch.setattr(ae, "gc", types.SimpleNamespace(collect=lambda: None))
    h = types.SimpleNamespace(
        batches=[], teardown_steps=[], fail=False, ae=ae, tmp_path=tmp_path
    )

    async def _loop(messages, state, ctx, **kw):
        batch = [
            m.get("content") if isinstance(m, dict) else m for m in messages
        ]
        h.batches.append(batch)
        if h.fail:
            raise RuntimeError("модель недоступна")
        return {"finished": True, "batch": batch}

    original_teardown = ae._aclose_mcp_and_release

    async def _teardown(owner_reset):
        if h.teardown_steps:
            await h.teardown_steps.pop(0)()
        await original_teardown(owner_reset)

    monkeypatch.setattr(ae, "arun_agent", _loop)
    monkeypatch.setattr(ae, "_aclose_mcp_and_release", _teardown)
    h.ctx = await acreate_run_context(_cfg(tmp_path), auth_connector=None)
    h.registry = AgentRunRegistry()
    h.threads = Threads(FileThreadStore(tmp_path))
    return h


async def _submit(env, text: str) -> None:
    await env.threads.submit(KEY, {"role": "user", "content": text})


async def _play(env):
    """Прогон задачи из входящего потока; дождаться его конца."""
    finished: list = []

    async def on_start(_run):
        return None

    async def on_finish(run):
        finished.append(run)

    run = await env.registry.astart(
        system=KEY.system,
        task_id=KEY.thread,
        executor=env.ae.AgentExecutor(env.ctx),
        ctx=env.ctx,
        on_start=on_start,
        on_finish=on_finish,
    )
    await asyncio.wait_for(
        asyncio.gather(run.task, return_exceptions=True), _BOUND_SEC
    )
    return finished[0]


async def _undelivered(env) -> int:
    record = await env.threads.get(KEY)
    return record.max_seq.get("inbox", 0) - record.state.inbox_seq


async def test_message_arriving_during_teardown_is_played_by_the_same_run(
    env,
):
    await _submit(env, "первый")

    async def _late():
        await _submit(env, "поздний")

    env.teardown_steps.append(_late)
    run = await _play(env)
    assert run.ending == "completed"
    assert env.batches == [["первый"], ["поздний"]]
    assert await _undelivered(env) == 0


async def test_message_in_the_window_after_the_handshake_is_played(
    env, monkeypatch
):
    """Исполнитель уже проверил поток (пусто), реестр ещё не снял прогон —
    вебхук видит прогон живым и отвечает ``queued``. Сообщение доигрывает
    проверка реестра после снятия прогона."""
    await _submit(env, "первый")
    real = env.ae.AgentExecutor.ainbox_left
    calls = [0]

    async def ainbox_left(self):
        calls[0] += 1
        if calls[0] == 2:  # проверка реестра после первой проверки хода
            await _submit(env, "в окне")
        return await real(self)

    monkeypatch.setattr(env.ae.AgentExecutor, "ainbox_left", ainbox_left)
    await _play(env)
    assert env.batches == [["первый"], ["в окне"]]
    assert await _undelivered(env) == 0


async def test_stopped_run_leaves_its_tail_to_the_next_run(env, monkeypatch):
    """Остановленный прогон хвост не доигрывает (это отменило бы остановку),
    но и не теряет: сообщение ждёт в потоке следующего прогона."""
    await _submit(env, "первый")
    holder: dict = {}
    real_init = env.ae.AgentExecutor.__init__

    def remember(self, ctx):
        real_init(self, ctx)
        holder["ex"] = self

    monkeypatch.setattr(env.ae.AgentExecutor, "__init__", remember)

    async def _stop_and_send():
        holder["ex"].stop_run()
        holder["ex"]._state.stop_requested = True
        await _submit(env, "после стопа")

    env.teardown_steps.append(_stop_and_send)
    await _play(env)
    assert env.batches == [["первый"]]
    assert await _undelivered(env) == 1

    await _play(env)
    assert env.batches[-1] == ["после стопа"]
    assert await _undelivered(env) == 0


async def test_failed_run_keeps_the_message_for_the_next_run(env):
    await _submit(env, "первый")
    env.fail = True
    run = await _play(env)
    assert run.ending == "failed"
    assert await _undelivered(env) == 1

    env.fail = False
    run = await _play(env)
    assert run.ending == "completed"
    assert env.batches[-1] == ["первый"]
    assert await _undelivered(env) == 0
