"""Сообщение, пришедшее прогону на излёте, доставляется, а не пропадает.

Цикл агента вычерпывает очередь в последний раз при закрытии хода, а прогон
после этого ещё сохраняет тред и сносит MCP-сессии — через `await`. Всё это
время прогон в реестре «жив»: вебхук кладёт сообщение в его исполнитель и
отвечает `queued`. Дальше его никто не читал — реестр ставил `done`, а на
следующий старт сервер строит НОВЫЙ исполнитель.

Исполнитель настоящий — `AgentExecutor` с его `arun` и очередью; подменены
только цикл агента (записывает батчи) и снос MCP (двойник выполняет шаг теста
ровно в окне сноса).
"""
import asyncio
import contextlib
import importlib
import logging
import types

import pytest

from gc_server.rest_api.agent_execution_registry import AgentRunRegistry

#: Потолок любого ожидания: зависший прогон должен дать красный тест, а не
#: вечный набор.
_BOUND_SEC = 5.0


def _within(awaitable):
    return asyncio.wait_for(awaitable, _BOUND_SEC)


def _cfg(tmp_path):
    return {
        "threads_data_path": str(tmp_path),
        "backend_api_mode": "chat_completions",
        "completions_url": "http://test",
        "model": "x",
        "max_steps": 1,
        "system_prompt_id": "v1",
    }


def _msg(content: str) -> dict:
    return {"role": "user", "content": content, "id": f"m-{content}"}


@pytest.fixture()
async def env(monkeypatch, tmp_path):
    from gc_core.run_context import acreate_run_context

    ae = importlib.import_module("gc_core.agent_executor")
    monkeypatch.setattr(ae, "gc", types.SimpleNamespace(collect=lambda: None))
    h = types.SimpleNamespace(batches=[], teardown_steps=[], fail=False)

    async def _loop(messages, state, ctx, **kw):
        h.batches.append(
            [m.get("content") if isinstance(m, dict) else m for m in messages]
        )
        if h.fail:
            raise RuntimeError("модель недоступна")
        return {"finished": True}

    original_teardown = ae._aclose_mcp_and_release

    async def _teardown(owner_reset):
        if h.teardown_steps:
            await h.teardown_steps.pop(0)()
        await original_teardown(owner_reset)

    monkeypatch.setattr(ae, "arun_agent", _loop)
    monkeypatch.setattr(ae, "_aclose_mcp_and_release", _teardown)
    h.ctx = await acreate_run_context(_cfg(tmp_path), auth_connector=None)
    h.executor_cls = ae.AgentExecutor
    h.registry = AgentRunRegistry()
    return h


async def _start(env, executor_factory=None):
    return await env.registry.astart_if_absent(
        system="s", task_id="t",
        executor_factory=executor_factory or env.executor_cls,
        ctx=env.ctx, initial_user_message="первый", thread_id="t",
    )


async def test_message_arriving_during_teardown_is_delivered_by_the_next_run(env):
    entered, release = asyncio.Event(), asyncio.Event()

    async def _slow_teardown():
        entered.set()
        await release.wait()

    env.teardown_steps.append(_slow_teardown)
    run = await _start(env)
    await _within(entered.wait())

    accepted = env.registry.add_user_message(
        system="s", task_id="t", make_message=_msg, content="вдогонку"
    )
    release.set()
    await _within(run.thread)

    assert accepted is True, "прогон в окне сноса ещё жив — сообщение принимается"
    assert env.batches == [["первый"], ["вдогонку"]], (
        f"принятое как queued сообщение не дошло до агента отдельным ходом: "
        f"{env.batches}"
    )
    assert run.done and run.error is None


async def test_run_is_closed_in_the_same_step_that_finds_no_tail(env):
    """Между «хвоста нет» и `done` нет ни одного `await`.

    Иначе вебхук, успевший в эту щель, получил бы `queued`, а его сообщение
    легло бы в исполнитель, который никто уже не прочтёт. Вебхук сажаем ровно
    туда: на первый же шаг loop после проверки хвоста.
    """
    loop = asyncio.get_running_loop()
    late: list = []

    def _late_webhook():
        late.append(env.registry.add_user_message(
            system="s", task_id="t", make_message=_msg, content="в щель"
        ))

    def _factory(ctx):
        executor = env.executor_cls(ctx)
        take = executor.take_undelivered_messages

        def _take_and_let_a_webhook_in():
            tail = take()
            if not tail and not late:
                loop.call_soon(_late_webhook)
            return tail

        executor.take_undelivered_messages = _take_and_let_a_webhook_in
        return executor

    run = await _start(env, _factory)
    await _within(run.thread)
    await asyncio.sleep(0)

    assert late, "вебхук так и не пришёл — тест ничего не проверил"
    delivered = [content for batch in env.batches for content in batch]
    assert late == [False] or "в щель" in delivered, (
        "вебхуку ответили queued уже после проверки хвоста, и сообщение пропало"
    )


@pytest.mark.parametrize("ending", ["stop", "failure"])
async def test_stopped_or_failed_run_does_not_replay_its_tail(env, caplog, ending):
    """Остановленный или упавший прогон хвост не доигрывает — но и не молчит.

    Доиграть хвост после `stop` значило бы проигнорировать остановку; после
    отказа — спрятать отказ за статусом следующего хода. Потеря такого
    сообщения — часть исхода прогона, и в логе у неё есть имя.
    """
    registry = env.registry

    async def _message_then_end():
        registry.add_user_message(
            system="s", task_id="t", make_message=_msg, content="вдогонку"
        )
        if ending == "stop":
            registry.stop(system="s", task_id="t")

    env.teardown_steps.append(_message_then_end)
    env.fail = ending == "failure"

    with caplog.at_level(logging.WARNING):
        run = await _start(env)
        await _within(run.thread)

    assert env.batches == [["первый"]], f"хвост доигран после {ending}: {env.batches}"
    assert "run_tail_dropped" in caplog.text, "сообщение, принятое как queued, исчезло молча"
    assert (run.error is not None) is (ending == "failure")


def _dropped(caplog) -> list:
    return [
        (r.ending, r.count)
        for r in caplog.records
        if getattr(r, "event", None) == "run_tail_dropped"
    ]


@pytest.mark.parametrize("ending", ["failed", "cancelled"])
async def test_tail_of_a_turn_that_dies_in_setup_is_logged_as_lost(
    env, monkeypatch, caplog, ending
):
    """Хвост, отданный повторному ходу, не исчезает без следа, если ход умер в сетапе.

    Для исполнителя батч хвоста — аргументы `arun`, а не очередь: снять хвост
    заново после падения значило получить пустоту. Клиенту на эти сообщения
    уже ответили `queued`, так что потеря обязана остаться в логе — с исходом
    и числом, как у хвоста остановленного прогона.

    Умирает здесь второй хвостовой ход, на чтении треда: падает или висит,
    пока прогон не снимут. Первый хвост доставлен и в потерю не входит.
    """
    ae = importlib.import_module("gc_core.agent_executor")
    real_session = ae.ThreadSession
    reads: list = []
    in_setup = asyncio.Event()

    async def _read_thread(**kw):
        reads.append(kw["thread_id"])
        if len(reads) == 3:
            if ending == "failed":
                raise OSError("тред не читается")
            in_setup.set()
            await asyncio.Event().wait()
        return await real_session.afrom_config(**kw)

    monkeypatch.setattr(
        ae, "ThreadSession", types.SimpleNamespace(afrom_config=_read_thread)
    )

    def _message(text: str):
        async def _step():
            env.registry.add_user_message(
                system="s", task_id="t", make_message=_msg, content=text
            )

        return _step

    env.teardown_steps += [_message("хвост-1"), _message("хвост-2")]
    with caplog.at_level(logging.WARNING):
        run = await _start(env)
        if ending == "cancelled":
            await _within(in_setup.wait())
            run.thread.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _within(run.thread)

    assert env.batches == [["первый"], ["хвост-1"]], (
        f"ход с вторым хвостом дошёл до цикла — тест ничего не проверил: {env.batches}"
    )
    assert _dropped(caplog) == [(ending, 1)], (
        f"потеря хвоста умершего хода посчитана неверно: {_dropped(caplog)}"
    )
    assert run.cancelled is (ending == "cancelled")
