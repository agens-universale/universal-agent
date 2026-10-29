"""Публичный async-фасад SDK (план async-переезда, Задача 17)."""

import asyncio
import threading
import time

import pytest
from gc_core.aio import AsyncAgent
from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    install,
    install_many,
    judge_accept,
    parity_config,
)


def _finish(report: str, call_id: str = "c2") -> dict:
    return assistant_tool_call(
        "turn_finish",
        {
            "solution_type": "direct_answer",
            "task_report": report,
            "handoff": "{}",
        },
        call_id=call_id,
    )


def _scenario(report: str = "готово") -> list[dict]:
    return [_finish(report), judge_accept()]


@pytest.fixture(autouse=True)
def _stub_turn_finish(monkeypatch):
    """Диспетчер тулов для ``turn_finish``: у стенда нет tools_data.

    Понадобилось после правки develop «батч с turn_finish — сначала вся work,
    закрытие по output»: ход теперь ИСПОЛНЯЕТ финализатор и закрывается по
    факту ``ok``, а не по обнаружению вызова в трассе. Без tools_data
    исполнение даёт unknown tool, ход не закрывается и упирается в
    ``step_limit``. develop завёл ровно такую же фикстуру у своих тестов.
    """
    from gc_core import processing_responses as pr
    from helpers.turn_finish_apply import stub_turn_finish_apply

    monkeypatch.setattr(pr, "aapply_tool", stub_turn_finish_apply)


async def test_facade_runs_a_turn(tmp_path, monkeypatch):
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(_scenario("42")))

    async with await AsyncAgent.create(cfg) as agent:
        answer = await agent.run("сколько будет 21*2", thread_id="t1")

    assert "42" in str(answer)


async def test_facade_does_not_block_the_loop(tmp_path, monkeypatch):
    """Смысл всего переезда: пока агент работает, loop живёт.

    Соседняя корутина тикает каждые 10 мс; максимальный разрыв между тиками
    показывает, залипал ли loop.
    """
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(_scenario()))

    gaps: list[float] = []
    stop = asyncio.Event()

    async def _heartbeat() -> None:
        prev = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - prev)
            prev = now

    # Сборщик глушится на окне: весь ход ложится в ОДИН разрыв, поэтому любая
    # пауза поколения 2 (180–220 мс на куче полного набора) попадает прямо в
    # измеряемый интервал и судится как залипание ядра.
    from gc_testkit import quiet_gc_for_loop_timing

    with quiet_gc_for_loop_timing():
        beat = asyncio.create_task(_heartbeat())
        # Даём пульсу завестись ДО прогона. Иначе первый assert проверяет не
        # свободу loop, а длительность прогона: на скриптованном бэкенде хода
        # хватает на 20-30 мс, и при шаге пульса 10 мс единственный тик мог
        # прийтись на любую паузу внутри — например на то самое `to_thread`,
        # которым раньше закрывались stdio-сессии. Убрали поток — тиков не стало,
        # хотя loop стал свободнее, а не занятее.
        #
        # С прогревом инструмент строже: тики есть гарантированно, а весь ход
        # ложится в ОДИН разрыв между ними — его и судит порог ниже.
        while not gaps:
            await asyncio.sleep(0.005)

        async with await AsyncAgent.create(cfg) as agent:
            await agent.run("привет", thread_id="t1")
        stop.set()
        await beat

    assert gaps, "хартбит не тикал вовсе"
    assert max(gaps) < 0.5, f"loop залипал на {max(gaps):.3f}с"


async def test_two_agents_run_concurrently(tmp_path, monkeypatch):
    """Два агента на одном loop — без потока на каждого.

    По сценарию на агента: общий раздаёт ходы в порядке вызовов, и с тех
    пор как запрос к модели идёт своей задачей (стоп develop снимает его),
    второй агент забирал судью первого — пятый запрос вместо четырёх."""
    cfg_a = parity_config(
        tmp_path / "a", monkeypatch, **{"agent.model": "агент-а"}
    )
    cfg_b = parity_config(
        tmp_path / "b", monkeypatch, **{"agent.model": "агент-б"}
    )
    scripts = install_many(
        monkeypatch,
        {
            "агент-а": ScriptedBackend(_scenario("раз")),
            "агент-б": ScriptedBackend(_scenario("два")),
        },
    )

    before = threading.active_count()
    a = await AsyncAgent.create(cfg_a)
    b = await AsyncAgent.create(cfg_b)
    async with a, b:
        r1, r2 = await asyncio.gather(
            a.run("раз", thread_id="t1"),
            b.run("два", thread_id="t2"),
        )

    assert r1 and r2
    for script in scripts.values():
        script.assert_clean()
    # Прогон агента больше не держит по потоку на себя; допуск на служебные
    # потоки httpx/логгера, но не на «поток на каждый прогон».
    assert threading.active_count() <= before + 2


def _finish_with_prose(prose: str, report: str = "готово") -> dict:
    """Ход модели: текст ответа И финализатор в одном сообщении.

    Голый ``_finish`` несёт ``content: None``, поэтому content-дельт у него нет
    вовсе — на нём стриминг проверить нельзя. Провайдеры такую форму отдают
    штатно: проза плюс вызов тула.
    """
    turn = _finish(report)
    turn["choices"][0]["message"]["content"] = prose
    return turn


async def test_facade_streams_deltas(tmp_path, monkeypatch):
    """stream() отдаёт дельты ответа и доигрывает ход до конца."""
    cfg = parity_config(tmp_path, monkeypatch)
    install(
        monkeypatch,
        ScriptedBackend([_finish_with_prose("считаю ответ"), judge_accept()]),
    )

    seen = []
    async with await AsyncAgent.create(cfg) as agent:
        async for kind, text in agent.stream("привет", thread_id="t1"):
            seen.append((kind, text))

    # Утверждаем СОСТАВ, а не тип. Прежнее `assert isinstance(seen, list)`
    # оставалось зелёным, даже когда доставка дельт вырезана целиком: мутация
    # строки `loop.call_soon_threadsafe(...)` в AsyncAgent.create не роняла ни
    # одного теста — у стримингового API SDK не было гейта вовсе.
    assert seen, "stream() не отдал ни одной дельты"
    assert "answer" in [kind for kind, _ in seen], seen
    answer = "".join(text for kind, text in seen if kind == "answer")
    assert answer == "считаю ответ", answer


async def test_stream_propagates_run_error(tmp_path, monkeypatch):
    """Ошибка прогона поднимается из итератора, а не теряется в тишине."""
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(_scenario()))

    async with await AsyncAgent.create(cfg) as agent:

        async def _boom(*_a, **_k):
            raise RuntimeError("прогон сломался")

        monkeypatch.setattr(agent.executor, "arun", _boom)
        with pytest.raises(RuntimeError, match="прогон сломался"):
            async for _ in agent.stream("привет", thread_id="t1"):
                pass


async def test_stop_is_idempotent(tmp_path, monkeypatch):
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(_scenario()))

    async with await AsyncAgent.create(cfg) as agent:
        agent.stop()
        agent.stop()


async def test_send_before_run_is_buffered(tmp_path, monkeypatch):
    """send() до прогона не падает: сообщение ляжет в очередь исполнителя."""
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(_scenario()))

    async with await AsyncAgent.create(cfg) as agent:
        agent.send({"role": "user", "content": "вдогонку"})


async def test_create_rejects_a_flat_dict():
    """Плоский словарь фасад не принимает: только AppConfig/LoadedSettings.

    Словарь — изменяемый и непровалидированный; правка настроек должна идти
    явно, через ``override_settings``. Отказ — сразу и с подсказкой, какой
    тип нужен, а не невнятное падение где-то в глубине сборки контекста.
    """
    with pytest.raises(TypeError, match="AppConfig"):
        await AsyncAgent.create({"model": "m"})
