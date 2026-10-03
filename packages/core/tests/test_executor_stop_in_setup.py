"""Стоп, пришедший в сетапе ВТОРОГО хода, не теряется.

`stop_run` пишет флаг в `_state`, а если state ещё нет — в отложенный, и его
применяет создание нового state. Но `_state` после хода не сбрасывался: у
исполнителя, который ведёт второй ход (повторный `arun` того же объекта —
хвост прогона у сервера и SDK, следующий запрос в TUI), он оставался от
прошлого. Стоп в сетапе — чтение треда, сборка состояния — уходил в тот
мёртвый state, новый стартовал чистым, и ход доигрывался целиком: с вызовами
модели и тулов, уже после того, как человек нажал стоп.

Каркас как в `test_executor_undelivered_tail.py`: настоящие
`acreate_run_context` и `AgentExecutor.arun`, подменён только цикл агента.
"""

import importlib
import types

from gc_testkit.settings import make_settings


def _cfg(tmp_path):
    return make_settings(
        {
            "threads_data_path": str(tmp_path),
            "backend_api_mode": "chat_completions",
            "completions_url": "http://test",
            "model": "x",
            "max_steps": 1,
            "system_prompt_id": "v1",
            "save_logs": False,
        }
    )


async def _executor(monkeypatch, tmp_path, seen: list):
    from gc_core.run_context import acreate_run_context

    ae = importlib.import_module("gc_core.agent_executor")
    monkeypatch.setattr(ae, "gc", types.SimpleNamespace(collect=lambda: None))

    async def _loop(messages, state, ctx, **kw):
        # Настоящий цикл спрашивает стоп через `check_stop` — фиксируем ответ,
        # каким его видит ход, которому передали управление.
        check_stop = kw.get("check_stop")
        seen.append(
            {
                "batch": [
                    m.get("content") if isinstance(m, dict) else m
                    for m in messages
                ],
                "stopped": bool(check_stop())
                if callable(check_stop)
                else None,
            }
        )
        return {"finished": True}

    monkeypatch.setattr(ae, "arun_agent", _loop)
    ctx = await acreate_run_context(_cfg(tmp_path), auth_connector=None)
    return ae.AgentExecutor(ctx), ae


async def test_stop_in_the_setup_of_a_second_turn_is_not_lost(
    monkeypatch, tmp_path
):
    """Стоп во время сетапа второго хода останавливает именно этот ход."""
    seen: list = []
    ex, ae = await _executor(monkeypatch, tmp_path, seen)

    await ex.arun(
        ["первый"], thread_id="t", start_new_thread=True, system="cli"
    )
    assert [t["batch"] for t in seen] == [["первый"]], (
        f"первый ход не дошёл до цикла: {seen}"
    )

    real_session = ae.ThreadSession
    reads: list = []

    async def _read_thread(**kw):
        reads.append(kw["thread_id"])
        # Подмена стоит только на второй ход, так что это его сетап: человек
        # жмёт стоп, пока исполнитель читает тред.
        if len(reads) == 1:
            ex.stop_run()
        return await real_session.afrom_config(**kw)

    monkeypatch.setattr(
        ae, "ThreadSession", types.SimpleNamespace(afrom_config=_read_thread)
    )

    await ex.arun(["второй"], thread_id="t", system="cli")

    assert reads, "подмена чтения треда не сработала — стоп не был позван"
    assert seen[-1]["batch"] == ["второй"], f"второй ход не состоялся: {seen}"
    assert seen[-1]["stopped"] is True, (
        f"стоп в сетапе потерян: ход стартовал, не зная о нём, и доиграет целиком — с вызовами модели и тулов ({seen})"
    )


async def test_stop_between_turns_does_not_leak_into_the_next_one(
    monkeypatch, tmp_path
):
    """А стоп, пришедший когда ход УЖЕ закончился, следующему не достаётся.

    Иначе нажатие стопа по остывшему прогону молча гасило бы следующий запрос
    человека — ровно та же потеря, только с другой стороны.
    """
    seen: list = []
    ex, _ = await _executor(monkeypatch, tmp_path, seen)

    await ex.arun(
        ["первый"], thread_id="t", start_new_thread=True, system="cli"
    )
    ex.stop_run()  # ход уже завершён, ничего не идёт

    await ex.arun(["второй"], thread_id="t", system="cli")

    assert [t["batch"] for t in seen] == [["первый"], ["второй"]], seen
    assert seen[-1]["stopped"] is False, (
        f"следующий запрос остановлен стопом от прошлого, остывшего хода: {seen}"
    )
