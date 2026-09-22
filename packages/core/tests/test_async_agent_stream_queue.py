"""Гейт: очередь ``stream()`` — на вызов, а не на агента.

Раньше ``asyncio.Queue`` заводилась один раз в ``AsyncAgent.create`` и жила
столько же, сколько агент. Отсюда два бага, оба воспроизводились замером:

1. после обычного ``run()`` в очереди оставались дельты, и следующий
   ``stream()`` выдавал их первыми — замер дал ``'ПЕРВЫЙ-ХОДВТОРОЙ-ХОД'``;
2. два ``stream()`` параллельно: первый забирал всё, включая чужие дельты,
   второй получал пустой список.

Плюс агент копил события ВСЕГДА, даже когда их никто не читал.

Тесты дёргают фасад напрямую, без сети и модели: ``run`` подменён, а дельты
кладутся тем же путём, каким их кладёт ядро — через ``ctx.on_stream_delta``.
"""
import asyncio

import pytest
from gc_core.aio.agent import AsyncAgent


class _FakeExecutor:
    """Исполнитель, который вместо хода просто сыплет дельты в sink."""

    def __init__(self) -> None:
        self.sink = None
        self.script: list[tuple[str, str]] = []
        self.answer = "ответ"

    async def arun(self, messages, **kwargs):
        for kind, text in self.script:
            self.sink(kind, text)
            await asyncio.sleep(0)      # дать петле разнести событие
        return self.answer


def _agent(script, answer="ответ"):
    """Собрать AsyncAgent руками, повторив проводку из create()."""
    subscribers: set = set()
    loop = asyncio.get_running_loop()

    def _fanout(item):
        for q in tuple(subscribers):
            q.put_nowait(item)

    def _sink(kind, text):
        if subscribers:
            loop.call_soon_threadsafe(_fanout, (kind, text))

    ex = _FakeExecutor()
    ex.sink = _sink
    ex.script = script
    ex.answer = answer
    return AsyncAgent(ex, subscribers), ex


@pytest.mark.asyncio
async def test_run_does_not_leak_deltas_into_the_next_stream():
    """Баг №1: дельты прошлого run() не должны прилетать в следующий stream()."""
    agent, ex = _agent([("answer", "ПЕРВЫЙ-ХОД")])

    await agent.run("вопрос")                    # никто не подписан

    ex.script = [("answer", "ВТОРОЙ-ХОД")]
    got = [text async for kind, text in agent.stream("вопрос")]

    assert got == ["ВТОРОЙ-ХОД"], f"в stream() протекли чужие дельты: {got}"


@pytest.mark.asyncio
async def test_two_streams_do_not_starve_each_other():
    """Баг №2: параллельные ``stream()`` не растаскивают дельты по одному.

    Раньше очередь была ОДНА на агента, и подписчики её ДЕЛИЛИ: первый забирал
    всё, второй получал пустой список. Теперь события размножаются по всем
    живым подписчикам, и пустым не остаётся никто.

    ЧЕГО ЭТОТ ТЕСТ НЕ ТРЕБУЕТ. Два параллельных ``stream()`` на ОДНОМ агенте
    запускают ДВА хода, а sink живёт на ``ctx`` и о ходах не знает — поэтому
    каждый подписчик видит дельты обоих. Разделять их пришлось бы sink'ом на
    прогон, но два одновременных хода на одном ``AgentExecutor`` и так не
    поддержаны (``_state`` там один). Для двух независимых потоков событий
    нужны два агента.
    """
    agent, _ = _agent([("answer", "a"), ("answer", "b")])

    async def collect():
        return [t async for _k, t in agent.stream("вопрос")]

    first, second = await asyncio.gather(collect(), collect())

    assert first and second, (
        f"один из stream() остался пустым — очередь делится, а не размножается: "
        f"{first} / {second}"
    )
    assert first == second, (
        f"подписчики увидели разное — веер работает не на всех: {first} / {second}"
    )
    assert set(first) == {"a", "b"}, f"потерялись дельты: {first}"


@pytest.mark.asyncio
async def test_nothing_accumulates_without_subscribers():
    """Без подписчиков агент не копит события — раньше копил всегда."""
    agent, _ = _agent([("answer", "мусор")] * 5)

    await agent.run("вопрос")

    assert not agent._subscribers, "остались висячие подписчики"
    got = [t async for _k, t in agent.stream("вопрос")]
    assert got == ["мусор"] * 5, f"накопленное протекло: {got}"


@pytest.mark.asyncio
async def test_stream_exposes_the_final_answer():
    """Финальный ответ больше не теряется.

    Async-генератор не умеет вернуть значение итерирующему, поэтому результат
    хода кладётся в ``last_answer``. Раньше ``await task`` в ``finally`` его
    просто отбрасывал, и потребителю приходилось склеивать ответ из дельт — а
    это не то же самое: ход закрывается ``turn_finish``, и прозы перед ним
    может не быть вовсе.
    """
    agent, _ = _agent([("answer", "часть")], answer="ПОЛНЫЙ ОТВЕТ")

    assert agent.last_answer is None
    async for _ in agent.stream("вопрос"):
        pass
    assert agent.last_answer == "ПОЛНЫЙ ОТВЕТ"


@pytest.mark.asyncio
async def test_abandoned_iteration_unsubscribes():
    """Брошенный итератор не должен оставлять очередь, куда копятся дельты."""
    agent, _ = _agent([("answer", "1"), ("answer", "2"), ("answer", "3")])

    it = agent.stream("вопрос")
    async for _ in it:
        break                       # бросаем на первой же дельте
    await it.aclose()

    assert not agent._subscribers, "подписчик остался после броска итерации"
