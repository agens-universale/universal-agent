"""Сообщения, положенные до старта прогона, доходят до агента.

Два разных механизма теряли их по дороге, и оба чинятся здесь.

1. `arun` в setup делал `self._pending_messages = []`. Сброс старше сервера:
   он появился, когда setup-фазы не было и класть сообщения заранее было
   некому. Теперь кладут — бронь сервера (клиенту уже ответили `queued`),
   `_prebuild_messages` в TUI, `AsyncAgent.send`, — и всё это стиралось.

2. Первый опрос очереди в цикле делал `messages = pending`, то есть ПОДМЕНЯЛ
   стартовый батч. Сообщение, пришедшее между стартом прогона и первым
   запросом к модели, вытесняло стартовое: оно не доходило ни до модели, ни до
   треда.

Каркас честный: настоящие `acreate_run_context` и `AgentExecutor.arun`,
подменён только `arun_agent`. Значит и сброс в setup, и опрос очереди
участвуют по-настоящему. И ассертим мы СЪЕДЕННОЕ циклом, а не «долежало до
атрибута исполнителя»: прошлый сторож зеленел именно на этой подмене.
"""

import importlib
import types

from gc_testkit.settings import make_settings


def _cfg(tmp_path):
    return make_settings(
        {
            "paths.threads_data_path": str(tmp_path),
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://test",
            "agent.model": "x",
            "agent.max_steps": 1,
            "agent.system_prompt_id": "v1",
            "logging.save_logs": False,
        }
    )


async def _executor(monkeypatch, tmp_path, on_call):
    """Настоящий executor; цикл агента подменён на `on_call(messages, poll)`."""
    from gc_core.run_context import acreate_run_context

    ae = importlib.import_module("gc_core.agent_executor")
    monkeypatch.setattr(ae, "gc", types.SimpleNamespace(collect=lambda: None))

    async def _stub(messages, state, ctx, **kw):
        await on_call(messages, kw.get("get_pending_messages"))
        return {"finished": True}

    monkeypatch.setattr(ae, "arun_agent", _stub)
    ctx = await acreate_run_context(_cfg(tmp_path), auth_connector=None)
    return ae.AgentExecutor(ctx)


def _texts(messages) -> list:
    out = []
    for m in messages or []:
        out.append(m.get("content") if isinstance(m, dict) else m)
    return out


async def test_message_queued_before_the_run_reaches_the_first_batch(
    monkeypatch, tmp_path
):
    """Положенное до `arun` уходит агенту в первом же запросе."""
    seen: list = []

    async def _on_call(messages, _poll):
        seen.append(_texts(messages))

    ex = await _executor(monkeypatch, tmp_path, _on_call)
    ex.add_message({"role": "user", "content": "из брони"})

    # `arun` принимает СТРОКИ (в очередь же кладут словари — так делает сервер).
    await ex.arun(
        ["стартовое"], thread_id="t", start_new_thread=True, system="cli"
    )

    assert seen and "из брони" in seen[0], (
        f"сообщение из очереди не доехало до модели: {seen}"
    )
    assert "стартовое" in seen[0], f"стартовое сообщение потеряно: {seen}"


# Второй механизм потери — первый опрос очереди ПОДМЕНЯЛ стартовый батч вместо
# слияния — проверяется в `test_processing_responses_context_baseline.py`
# (`test_first_poll_merges_pending_with_the_start_batch`): там есть каркас,
# гоняющий настоящую `arun_agent`.
#
# Здесь эта проверка стояла в первой редакции и была ХОЛОСТОЙ: она повторяла
# логику слияния внутри собственной заглушки и потому проверяла саму себя, а не
# цикл. Подмена это и показала — с откаченной правкой файл давал «3 passed».
# Оставлено как напоминание: тест, имитирующий проверяемый код, не сторож.


async def test_the_tail_does_not_leak_into_the_next_run(monkeypatch, tmp_path):
    """Несъеденное не протекает в следующий прогон, но и не пропадает молча.

    Это исходный смысл сброса, ради которого он стоял в setup: новый прогон не
    должен проигрывать хвост прошлого. Перенесён на выход из цикла.
    """
    seen: list = []
    holder: dict = {}

    async def _on_call(messages, _poll):
        seen.append(_texts(messages))
        if len(seen) == 1:
            # Сообщение приходит ВО ВРЕМЯ прогона, и цикл его не вычерпывает
            # (poll не зовём) — ровно так выглядит остановленный или упавший
            # прогон. Положить его ДО старта нельзя: теперь оно честно уедет в
            # первый батч, то есть будет доставлено, а не потеряно.
            holder["ex"].add_message({"role": "user", "content": "не съедено"})

    ex = await _executor(monkeypatch, tmp_path, _on_call)
    holder["ex"] = ex

    await ex.arun(
        ["первый"], thread_id="t", start_new_thread=True, system="cli"
    )
    await ex.arun(
        ["второй"], thread_id="t", start_new_thread=True, system="cli"
    )

    assert seen[1] == ["второй"], (
        f"хвост прошлого прогона протёк в следующий: {seen[1]}"
    )
    assert [m["content"] for m in ex.take_undelivered_messages()] == [
        "не съедено"
    ], "несъеденное пропало молча — а клиенту на него уже ответили «принято»"


async def test_a_message_queued_during_the_run_lifts_the_judge_pause(
    monkeypatch, tmp_path
):
    """Сообщение из очереди — такое же новое слово человека: снимает паузу
    судьи посреди прогона (вебхук в идущую задачу, реплика TUI)."""
    holder: dict = {}
    seen: dict = {}

    async def _on_call(_messages, poll):
        ex = holder["ex"]
        shared = ex._state.shared
        shared.judge_paused = True
        shared.judge_denials_in_a_row = 3
        ex.add_message({"role": "user", "content": "продолжай"})
        poll()
        seen["paused"] = shared.judge_paused
        seen["in_a_row"] = shared.judge_denials_in_a_row

    ex = await _executor(monkeypatch, tmp_path, _on_call)
    holder["ex"] = ex
    await ex.arun(
        ["стартовое"], thread_id="t", start_new_thread=True, system="cli"
    )
    assert seen == {"paused": False, "in_a_row": 0}
