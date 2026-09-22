"""Хвост хода SDK доигрывается своим ходом, а не остаётся в исполнителе.

Сервер и TUI забирают хвост (`take_undelivered_messages`) и доигрывают его
отдельным ходом. Третий владелец исполнителя — `AsyncAgent` — его не звал:
сообщение, отданное `send` после последнего забора очереди циклом, пока ход
сохранял тред и сносил MCP, оставалось в исполнителе. Ни один ход его оттуда
не забирал: следующий `run` отдал бы его позади своего запроса, а без
следующего оно не доходило вовсе.

Каркас — как в `test_executor_undelivered_tail.py`: настоящие
`AsyncAgent.create`, `acreate_run_context` и `AgentExecutor.arun`; подменены
только цикл агента (записывает батчи) и снос MCP (двойник выполняет шаг теста
ровно в окне сноса).
"""
import asyncio
import importlib
import logging
import types

import pytest

from gc_core.aio import AsyncAgent

#: Потолок ожидания в тестах: зависший ход — красный тест, а не вечный набор.
_BOUND_SEC = 5.0


def _cfg(tmp_path):
    return {
        "threads_data_path": str(tmp_path),
        "backend_api_mode": "chat_completions",
        "completions_url": "http://test",
        "model": "x",
        "max_steps": 1,
        "system_prompt_id": "v1",
    }


def _msg(text: str) -> dict:
    return {"role": "user", "content": text, "id": f"m_{text}"}


def _dropped(caplog) -> list:
    return [
        (r.ending, r.count)
        for r in caplog.records
        if getattr(r, "event", None) == "run_tail_dropped"
    ]


@pytest.fixture()
def env(monkeypatch):
    ae = importlib.import_module("gc_core.agent_executor")
    monkeypatch.setattr(ae, "gc", types.SimpleNamespace(collect=lambda: None))
    h = types.SimpleNamespace(batches=[], teardown_steps=[], calls=[], ae=ae)

    async def _loop(messages, state, ctx, **kw):
        batch = [m.get("content") if isinstance(m, dict) else m for m in messages]
        h.batches.append(batch)
        return {"answer": batch[-1]}

    original_teardown = ae._aclose_mcp_and_release

    async def _teardown(owner_reset):
        if h.teardown_steps:
            h.teardown_steps.pop(0)()
        await original_teardown(owner_reset)

    monkeypatch.setattr(ae, "arun_agent", _loop)
    monkeypatch.setattr(ae, "_aclose_mcp_and_release", _teardown)
    return h


async def _agent(tmp_path, env) -> AsyncAgent:
    """Агент, у которого записывается, в каком треде и как идёт каждый ход."""
    agent = await AsyncAgent.create(_cfg(tmp_path))
    arun = agent.executor.arun

    async def _recording_arun(messages, **kw):
        env.calls.append((kw.get("thread_id"), kw.get("start_new_thread", False)))
        return await arun(messages, **kw)

    agent.executor.arun = _recording_arun
    return agent


def _break_thread_read(monkeypatch, env, turn: int, on_read) -> None:
    """Чтение треда в сетапе хода номер `turn` сначала идёт в `on_read`."""
    real_session = env.ae.ThreadSession
    reads: list = []

    async def _read_thread(**kw):
        reads.append(kw["thread_id"])
        if len(reads) == turn:
            await on_read()
        return await real_session.afrom_config(**kw)

    monkeypatch.setattr(
        env.ae, "ThreadSession", types.SimpleNamespace(afrom_config=_read_thread)
    )


async def test_message_sent_during_teardown_reaches_the_agent_in_its_own_turn(
    tmp_path, env
):
    async with await _agent(tmp_path, env) as agent:
        env.teardown_steps.append(lambda: agent.send(_msg("вдогонку")))
        answer = await agent.run("первый", thread_id="t", start_new_thread=True)
        leftover = agent.executor.take_undelivered_messages()

    assert env.batches == [["первый"], ["вдогонку"]], (
        f"сообщение, отданное send на излёте хода, не дошло до агента: {env.batches}"
    )
    assert env.calls[1] == ("t", False), "хвост доигран не в том же треде или с чистого листа"
    assert leftover == [], "хвост доигран, но остался лежать в исполнителе"
    # `run` отдаёт ответ последнего хода: он идёт по тому же треду и отвечает
    # с учётом всего, что было до него.
    assert answer == {"answer": "вдогонку"}, answer


async def test_stopped_run_neither_replays_its_tail_nor_leaves_it(
    tmp_path, env, caplog
):
    """После стопа хвост не доигрывается — и не остаётся следующему `run`.

    Доиграть его значило бы проигнорировать остановку, а оставить в
    исполнителе — отдать следующему ходу позади его собственного запроса.
    Сам стоп следующему `run` тоже не достаётся: его хвост доигрывается.
    """
    async with await _agent(tmp_path, env) as agent:

        def _sent_then_stopped():
            agent.send(_msg("вдогонку"))
            agent.stop()

        env.teardown_steps.append(_sent_then_stopped)
        with caplog.at_level(logging.WARNING):
            await agent.run("первый", thread_id="t", start_new_thread=True)
        leftover = agent.executor.take_undelivered_messages()
        env.teardown_steps.append(lambda: agent.send(_msg("к второму")))
        await agent.run("второй", thread_id="t")

    assert env.batches[0] == ["первый"] and ["вдогонку"] not in env.batches, (
        f"хвост доигран после стопа: {env.batches}"
    )
    assert leftover == [], "хвост остановленного хода остался следующему run"
    assert _dropped(caplog) == [("stopped", 1)], _dropped(caplog)
    assert env.batches[1:] == [["второй"], ["к второму"]], (
        f"стоп прошлого run оборвал доигрывание следующего: {env.batches}"
    )


async def test_stop_in_the_setup_of_a_tail_turn_ends_the_replays(
    tmp_path, env, monkeypatch, caplog
):
    """Стоп, пришедший в сетап хвостового хода, обрывает доигрывание.

    Исполнитель такой стоп не видит: state нового хода ещё не создан, и
    `stop_run` помечает state закончившегося. Не держи фасад свой флаг, он
    доигрывал бы хвост за хвостом вопреки остановке.
    """
    async with await _agent(tmp_path, env) as agent:

        async def _stop_in_setup():
            agent.stop()

        _break_thread_read(monkeypatch, env, 2, _stop_in_setup)
        env.teardown_steps.append(lambda: agent.send(_msg("хвост-1")))
        env.teardown_steps.append(lambda: agent.send(_msg("хвост-2")))
        with caplog.at_level(logging.WARNING):
            await agent.run("первый", thread_id="t", start_new_thread=True)

    delivered = [text for batch in env.batches for text in batch]
    assert "хвост-2" not in delivered, f"доигрывание пережило стоп: {env.batches}"
    assert ("stopped", 1) in _dropped(caplog), _dropped(caplog)


@pytest.mark.parametrize("ending", ["failed", "cancelled"])
async def test_tail_of_a_turn_that_dies_in_setup_is_logged_as_lost(
    tmp_path, env, monkeypatch, caplog, ending
):
    """Батч хвоста, с которым умер ход, уходит в лог потерянным, а не в пустоту.

    Для исполнителя он — аргументы `arun`, а не очередь: снять хвост заново
    после падения значило получить пустоту. Умирает здесь второй хвостовой
    ход; первый хвост доставлен и в потерю не входит.
    """
    in_setup = asyncio.Event()

    async def _die_in_setup():
        if ending == "failed":
            raise OSError("тред не читается")
        in_setup.set()
        await asyncio.Event().wait()

    _break_thread_read(monkeypatch, env, 3, _die_in_setup)
    expected = OSError if ending == "failed" else asyncio.CancelledError
    async with await _agent(tmp_path, env) as agent:
        env.teardown_steps.append(lambda: agent.send(_msg("хвост-1")))
        env.teardown_steps.append(lambda: agent.send(_msg("хвост-2")))
        with caplog.at_level(logging.WARNING):
            run = asyncio.create_task(
                agent.run("первый", thread_id="t", start_new_thread=True)
            )
            if ending == "cancelled":
                await asyncio.wait_for(in_setup.wait(), _BOUND_SEC)
                run.cancel()
            with pytest.raises(expected):
                await asyncio.wait_for(run, _BOUND_SEC)

    assert env.batches == [["первый"], ["хвост-1"]], (
        f"ход со вторым хвостом дошёл до цикла — тест ничего не проверил: {env.batches}"
    )
    assert _dropped(caplog) == [(ending, 1)], (
        f"потеря хвоста умершего хода посчитана неверно: {_dropped(caplog)}"
    )
