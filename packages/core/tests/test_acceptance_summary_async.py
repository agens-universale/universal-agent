"""Приёмка суммаризации под async: компакция не ломает то, ради чего переезжали.

Суммаризация — единственная подсистема, у которой сегодня появился async-двойник
ВСЕЙ оркестрации, а не отдельного вызова. И она же самая опасная для цикла:
компакция — это дополнительные обращения к модели ВНУТРИ хода, плюс двухфазная
перезапись треда (архив + новая история).

Что проверяется здесь и чего нет в других наборах:

* ``test_acceptance_non_blocking`` меряет прогон БЕЗ компакции — порог там
  дефолтный и не перешагивается;
* ``test_summarization_no_thread`` проверяет компакцию без треда;
* ``test_thread_store_async`` — что синхронный и асинхронный пути дают одно и
  то же;
* здесь — поведение компакции ВО ВРЕМЯ работы цикла: треды, лаг, соседние
  агенты, входящее сообщение и отмена.

Порог занижен до 200 символов, чтобы компакция гарантированно срабатывала на
коротком сценарии. Что она действительно сработала — проверяется каждым тестом
отдельно, а не подразумевается: «порог выставлен» не значит «компакция была».
"""
import asyncio
import json
import threading
import time

import pytest

from gc_core.aio import AsyncAgent

from parity_harness import (
    ScriptedBackend,
    assistant_text,
    assistant_tool_call,
    install,
    install_many,
    judge_accept,
    parity_config,
)


@pytest.fixture(autouse=True)
def _stub_turn_finish(monkeypatch):
    """Подменяем ТОЛЬКО ``turn_finish``, остальные тулы идут по-настоящему.

    После правки develop «батч с turn_finish — сначала вся work, закрытие по
    output» ход ИСПОЛНЯЕТ финализатор и закрывается по факту ``ok``, а не по
    обнаружению вызова в трассе. У стенда нет tools_data, поэтому исполнение
    даёт unknown tool, ход не закрывается и упирается в ``step_limit``.

    Делегирующая, а не сплошная: эти тесты меряют ОТЗЫВЧИВОСТЬ во время
    настоящей работы. Сплошная заглушка сделала бы ход мгновенным, хартбит
    не успел бы ни разу тикнуть, и зелёный тест не значил бы ничего —
    наблюдалось: 0 тиков вместо сотни.
    """
    from gc_core import processing_responses as pr
    from helpers.turn_finish_apply import stub_turn_finish_apply

    real = pr.aapply_tool

    async def _dispatch(**kwargs):
        if kwargs.get("tool_name") == "turn_finish":
            return await stub_turn_finish_apply(**kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(pr, "aapply_tool", _dispatch)


TURNS = [
    assistant_tool_call("exec_calculator", {"expression": "2+2"}, call_id="c1"),
    assistant_tool_call("turn_finish", {
        "solution_type": "direct_answer", "task_report": "готово", "handoff": "{}",
    }, call_id="c2"),
    judge_accept(),
]

#: Компакция — это ДОПОЛНИТЕЛЬНЫЕ обращения к модели (сводка, memory-patch).
#: Без запаса сценарий исчерпывается, и прогон падает не по той причине.
TAIL = [assistant_text("сводка предыдущего диалога") for _ in range(8)]


def script() -> ScriptedBackend:
    return ScriptedBackend([*TURNS, *TAIL])


#: Для замера параллелизма нужен ход, который занимает ЗАМЕТНОЕ время: на
#: калькуляторе весь прогон укладывается в 40 мс, и отношение считает не
#: параллельность, а постоянные издержки старта.
SLOW_TURNS = [
    assistant_tool_call("exec_sleep", {"seconds": 0.3}, call_id="c1"),
    *TURNS[1:],
]


def slow_script() -> ScriptedBackend:
    return ScriptedBackend([*SLOW_TURNS, *TAIL])


def compacting_config(tmp_path, monkeypatch, **overrides):
    """Конфиг, при котором компакция гарантированно срабатывает."""
    cfg = parity_config(tmp_path, monkeypatch, **overrides)
    params = dict(cfg.get("summary_params") or {})
    params["chat_completions_summary_trigger_chars"] = 200
    params["chat_completions_summary_turn_end_trigger_chars"] = 120
    cfg["summary_params"] = params
    return cfg


def thread_dump(cfg, system_id, thread_id):
    from gc_core.thread_store import _thread_paths, resolve_threads_dir
    from pathlib import Path

    data_path, _ = _thread_paths(resolve_threads_dir(cfg), system_id, thread_id)
    path = Path(data_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def was_compacted(cfg, system_id, thread_id) -> bool:
    """Компакция была ⟺ история схлопнулась в одну сводку."""
    dump = thread_dump(cfg, system_id, thread_id)
    return (
        len(dump) == 1
        and "dialog_summary" in json.dumps(dump, ensure_ascii=False)
    )


# --- S-1: компакция не заводит потоков -------------------------------------

async def test_compaction_creates_no_threads(tmp_path, monkeypatch):
    """Компакция не создаёт ни одного потока.

    До переезда суммаризация уезжала в поток из пула на всё время LLM-вызова.
    Проверяем именно ИТОГ: сколько потоков было до прогона, столько и после.
    Оседающий поток на каждую компакцию — утечка, заметная только под нагрузкой.
    """
    async def run_and_snapshot(cfg, tid):
        install(monkeypatch, script())
        async with await AsyncAgent.create(cfg) as agent:
            await agent.run("сделай что-нибудь", thread_id=tid)
        # Демон-потоки сходят не мгновенно — даём им время.
        for _ in range(20):
            await asyncio.sleep(0.05)
        return {t.name for t in threading.enumerate()}

    # Контроль: обычный прогон БЕЗ компакции. Сравнивать надо с ним, а не с
    # абсолютным нулём: пул ``asyncio.to_thread`` заводит рабочий поток на
    # любом прогоне, и к компакции он отношения не имеет.
    plain_cfg = parity_config(tmp_path / "plain", monkeypatch)
    plain = await run_and_snapshot(plain_cfg, "t-s1-plain")
    assert not was_compacted(plain_cfg, "sdk", "t-s1-plain"), "контроль сам сжался"

    cfg = compacting_config(tmp_path / "compact", monkeypatch)
    after = await run_and_snapshot(cfg, "t-s1")

    assert was_compacted(cfg, "sdk", "t-s1"), "компакция не сработала — тест пуст"
    extra = after - plain
    assert not extra, (
        f"компакция оставила потоки, которых нет у обычного прогона: {sorted(extra)}. "
        "Так проявляется синхронный вызов на её пути: он уходит в поток или на "
        "портал вместо простого await."
    )


# --- S-2: цикл не залипает во время компакции ------------------------------

class _Pulse:
    """Пульс на loop: тикает каждые ``STEP`` и запоминает разрывы.

    Разрыв — это время, на которое loop НЕ вернулся к пульсу. Его источников
    два: занятость самого loop (то, что мы проверяем) и занятость машины (то,
    что нас не касается). Разделить их абсолютным порогом нельзя — под полным
    прогоном набора планировщик ОС не возвращает управление за 10 мс, сколько
    бы loop ни был свободен. Поэтому порог здесь не абсолютный, а относительно
    базы, снятой тем же пульсом в том же прогоне.
    """

    STEP = 0.01

    def __init__(self) -> None:
        self.gaps: list[float] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "_Pulse":
        self._task = asyncio.create_task(self._beat())
        # Ждём первый тик. Иначе замер начинается до старта пульса и меряет не
        # свободу цикла, а то, успел ли он вообще стартовать.
        while not self.gaps:
            await asyncio.sleep(self.STEP / 2)
        self.gaps.clear()
        return self

    async def __aexit__(self, *_exc) -> None:
        self._stop.set()
        await self._task

    async def _beat(self) -> None:
        prev = time.monotonic()
        while not self._stop.is_set():
            await asyncio.sleep(self.STEP)
            now = time.monotonic()
            self.gaps.append(now - prev)
            prev = now

    def p95(self) -> float:
        """Только для диагностики в сообщении об ошибке, НЕ критерий.

        Здоровый хвост рядом с разъехавшимся максимумом — это и есть картина
        «цикл отняли один раз надолго»; критерием процентиль здесь быть не может
        (см. докстринг ``test_loop_stays_responsive_during_compaction``).
        """
        assert self.gaps, "пульс не тикал вовсе"
        ordered = sorted(self.gaps)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]

    def worst(self) -> float:
        assert self.gaps, "пульс не тикал вовсе"
        return max(self.gaps)

    def report(self) -> str:
        import statistics

        return (
            f"p95 {self.p95() * 1000:.0f} мс, "
            f"медиана {statistics.median(self.gaps) * 1000:.0f} мс, "
            f"максимум {self.worst() * 1000:.0f} мс, тиков {len(self.gaps)}"
        )


#: Во сколько раз худший разрыв ВО ВРЕМЯ компакции может быть хуже базы. Запас
#: грубый намеренно: ловим не микросекунды, а отнятый цикл.
_BUDGET = 3.0

#: Пол на идеально тихой машине: при базе в 10 мс тройной запас дал бы 30 мс, и
#: тест падал бы от одной паузы GC. Выбран выше джиттера GC (десятки мс) и
#: заведомо ниже любого блокирующего вызова (сотни мс и больше).
_STALL_FLOOR = 0.25

#: Заведомая заморозка для контроля измерителя. Больше `_TAIL_FLOOR`, чтобы
#: контроль не зависел от того, каким вышел порог.
_CONTROL_FREEZE = 0.25


async def test_the_pulse_actually_sees_a_frozen_loop():
    """Контроль измерителя: без него зелёный вердикт нефальсифицируем.

    Пульс, который ничего не видит, одинаково зелёный и когда цикл свободен, и
    когда мы меряем не то. Здесь loop замораживается заведомо — синхронным
    ``time.sleep`` прямо в корутине, — и пульс ОБЯЗАН это заметить.

    Тот же приём, что на стенде приёмки (``~/gc-core-acceptance/control.py``),
    только дешёвый: одна заморозка вместо живого прогона.
    """
    async with _Pulse() as pulse:
        await asyncio.sleep(_Pulse.STEP * 3)
        time.sleep(_CONTROL_FREEZE)          # заморозка loop, не await
        await asyncio.sleep(_Pulse.STEP * 3)

    # Сравниваем с СОБСТВЕННОЙ медианой того же окна, а не с нулём. Первая
    # редакция требовала `worst >= 0.8 * freeze` — и это проходило само собой
    # на медленной машине, где типичный тик и так больше заморозки: проверка
    # была бы зелёной, ничего не проверяя. Разница «худший минус типичный»
    # изолирует именно заморозку и от шага пульса не зависит.
    #
    # Проверено подменой: замена `time.sleep` на `await asyncio.sleep` (то есть
    # заморозки нет) красит тест.
    import statistics

    excess = pulse.worst() - statistics.median(pulse.gaps)
    assert excess >= _CONTROL_FREEZE * 0.8, (
        f"пульс НЕ увидел заморозку на {_CONTROL_FREEZE * 1000:.0f} мс: "
        f"худший разрыв всего на {excess * 1000:.0f} мс больше типичного "
        f"({pulse.report()}) — измеритель сломан, и остальные его выводы "
        "ничего не значат"
    )


async def test_loop_stays_responsive_during_compaction(tmp_path, monkeypatch):
    """Цикл жив на всём протяжении компакции.

    ``test_acceptance_non_blocking`` меряет лаг на прогоне БЕЗ компакции: порог
    там дефолтный и не перешагивается. Здесь компакция происходит, и вместе с
    ней — двухфазная перезапись треда.

    Порог **относительный**: сначала тем же пульсом снимается база (loop ничем
    не занят), потом — разрывы во время компакции, и сравниваются они друг с
    другом. Абсолютные 150 мс держались, пока набор гоняли по одному; в полном
    прогоне машина занята, `asyncio.sleep(0.01)` сам по себе возвращается через
    130 мс, и тест падал на 249 мс — не потому, что цикл отняли, а потому что
    планировщику ОС было не до нас. Ловим ОТНОШЕНИЕ: занятость машины бьёт по
    базе и по замеру одинаково и из отношения уходит.

    Судим по ХУДШЕМУ разрыву, а не по процентилю. Процентиль здесь стоял ради
    защиты от шума — и ровно поэтому ничего не ловил: замеры показали, что при
    29 тиках `p95` вырождается во «второй по величине разрыв», а настоящая
    поломка даёт ОДИН длинный. Вставленный в компакцию `time.sleep(0.4)`
    поднимал максимум до 516 мс, оставляя `p95` на 46 мс, — тест с одним лишь
    процентилем оставался зелёным на живой поломке. Повторные заморозки при
    этом складываются в тот же один разрыв, так что случая, где процентиль
    срабатывает раньше максимума, построить не удалось. От шума защищает не
    процентиль, а база.

    Что измеритель вообще способен увидеть заморозку — отдельный тест выше.
    """
    cfg = compacting_config(tmp_path, monkeypatch)
    # МЕДЛЕННЫЙ сценарий, а не обычный. На скриптованном бэкенде сети нет, и
    # быстрый прогон укладывается в 90 мс — пульс успевает РОВНО ОДИН тик.
    # Процентиль по одной точке не значит ничего: прежний абсолютный порог в
    # 150 мс просто накрывал этот единственный замер, и «зелёный» тест не
    # отличал свободный цикл от занятого. `slow_script` даёт ход на 0.3 с через
    # `exec_sleep` — тул с нативным `asyncio.sleep`, то есть loop на это время
    # СВОБОДЕН и пульс успевает набрать выборку.
    install(monkeypatch, slow_script())

    # База: тот же пульс, тот же процесс, та же загрузка машины — но loop
    # свободен. Окно соизмеримо с прогоном, чтобы хвост считался по сравнимому
    # числу тиков.
    async with _Pulse() as baseline:
        await asyncio.sleep(0.3)

    async with _Pulse() as during:
        async with await AsyncAgent.create(cfg) as agent:
            await agent.run("сделай что-нибудь", thread_id="t-s2")

    assert was_compacted(cfg, "sdk", "t-s2"), "компакция не сработала — тест пуст"

    # Выборка обязана быть настоящей: на одном-двух тиках процентиль вырождается
    # в «худший разрыв», и тест снова начинает мерить шум вместо занятости.
    assert len(during.gaps) >= 10, (
        f"пульс успел всего {len(during.gaps)} тик(ов) за прогон "
        f"({during.report()}) — выборки нет, и вывод о свободе цикла делать "
        "не на чем. Сценарий стал быстрее? Верните ход, занимающий заметное "
        "время."
    )

    budget = max(_STALL_FLOOR, _BUDGET * baseline.worst())
    assert during.worst() < budget, (
        f"цикл отняли на {during.worst() * 1000:.0f} мс при бюджете "
        f"{budget * 1000:.0f} мс = max({_STALL_FLOOR * 1000:.0f} мс, "
        f"{_BUDGET:g}× базы)\n"
        f"во время компакции: {during.report()}\n"
        f"база (loop свободен):  {baseline.report()}"
    )


# --- S-3: два агента компактятся одновременно ------------------------------

async def test_two_agents_compact_concurrently(tmp_path, monkeypatch):
    """Две одновременные компакции не путают треды.

    Общий сценарий на двух агентов не годится — ходы раздаются в порядке
    ВЫЗОВОВ, и второй забрал бы финализатор первого. Поэтому у каждого свой
    сценарий, привязанный к его ``model``.
    """
    cfg_a = compacting_config(tmp_path / "a", monkeypatch, model="агент-а")
    cfg_b = compacting_config(tmp_path / "b", monkeypatch, model="агент-б")
    install_many(monkeypatch, {"агент-а": script(), "агент-б": script()})

    a = await AsyncAgent.create(cfg_a)
    b = await AsyncAgent.create(cfg_b)
    try:
        answers = await asyncio.gather(
            a.run("первый", thread_id="t-s3-a"),
            b.run("второй", thread_id="t-s3-b"),
        )
    finally:
        await a.aclose()
        await b.aclose()

    assert all(answers), "не все агенты ответили"
    for cfg, tid in ((cfg_a, "t-s3-a"), (cfg_b, "t-s3-b")):
        assert was_compacted(cfg, "sdk", tid), f"{tid}: компакция не сработала"

    # Архивы у каждого свои и не перемешались.
    from pathlib import Path

    for cfg, tid, other in ((cfg_a, "t-s3-a", "t-s3-b"), (cfg_b, "t-s3-b", "t-s3-a")):
        root = Path(cfg["threads_data_path"])
        archives = [p.name for p in root.glob("*__pre_summary__*")]
        assert archives, f"{tid}: архив компакции не создан"
        assert not any(other in name for name in archives), (
            f"в каталоге {tid} лежит архив чужого треда: {archives}"
        )


# --- S-4: компакция под нагрузкой соседа -----------------------------------

async def test_compaction_does_not_serialize_agents(tmp_path, monkeypatch):
    """Компакция одного агента не выстраивает второго в очередь.

    Критерий приёмки — параллелизм не хуже 1.6× от последовательного. Компакция
    держит цикл дольше обычного хода, поэтому именно на ней очередь заметнее
    всего.
    """
    solo_cfg = compacting_config(tmp_path / "solo", monkeypatch, model="соло")
    install_many(monkeypatch, {"соло": slow_script()})
    solo = await AsyncAgent.create(solo_cfg)
    started = time.monotonic()
    try:
        await solo.run("один", thread_id="t-s4-solo")
    finally:
        await solo.aclose()
    alone = time.monotonic() - started

    cfg_a = compacting_config(tmp_path / "pa", monkeypatch, model="пара-а")
    cfg_b = compacting_config(tmp_path / "pb", monkeypatch, model="пара-б")
    install_many(monkeypatch, {"пара-а": slow_script(), "пара-б": slow_script()})
    a = await AsyncAgent.create(cfg_a)
    b = await AsyncAgent.create(cfg_b)
    started = time.monotonic()
    try:
        await asyncio.gather(
            a.run("первый", thread_id="t-s4-a"),
            b.run("второй", thread_id="t-s4-b"),
        )
    finally:
        await a.aclose()
        await b.aclose()
    together = time.monotonic() - started

    assert was_compacted(cfg_a, "sdk", "t-s4-a"), "компакция не сработала — тест пуст"
    ratio = together / alone if alone else 0.0
    assert ratio < 1.6, (
        f"двое с компакцией шли {ratio:.2f}× от одиночного прогона "
        f"({together:.2f}с против {alone:.2f}с) — компакция сериализует агентов"
    )


# --- S-7: сообщение, пришедшее во время компакции --------------------------

async def test_message_arriving_during_compaction_is_not_lost(tmp_path, monkeypatch):
    """Сообщение, пришедшее во время компакции, не теряется.

    После turn-end компакции ядро добирает накопленное через
    ``get_pending_messages`` — контекст к этому моменту уже сжат. Проверяем не
    механику, а итог: сообщение доехало до истории треда.
    """
    cfg = compacting_config(tmp_path, monkeypatch)
    install(monkeypatch, script())

    async with await AsyncAgent.create(cfg) as agent:
        await agent.run("первый вопрос", thread_id="t-s7")
        assert was_compacted(cfg, "sdk", "t-s7"), "компакция не сработала"

    # Второй прогон — с ОБЫЧНЫМ порогом, но по тому же треду. Порог в 200
    # символов нужен был только чтобы вызвать компакцию: сама сводка длиннее
    # него, поэтому с ним каждый следующий прогон сжимался бы снова и снова,
    # и сценарий кончался бы раньше, чем проверяемое свойство.
    plain_cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, script())
    async with await AsyncAgent.create(plain_cfg) as agent:
        await agent.run("вопрос после компакции", thread_id="t-s7")

    dump = json.dumps(thread_dump(cfg, "sdk", "t-s7"), ensure_ascii=False)
    assert "вопрос после компакции" in dump, (
        "сообщение, отправленное после компакции, не попало в тред"
    )


# --- S-8: отмена посреди компакции -----------------------------------------

async def test_cancelled_compaction_leaves_the_thread_readable(tmp_path, monkeypatch):
    """Отмена посреди компакции не рвёт тред.

    Единственное место, где я заранее ждал сюрприза: замещение истории
    двухфазное (архив плюс новая история), и на файловом хранилище атомарным
    оно не является.

    Проверяется не «компакция доехала» — она имеет право не доехать, — а что
    ПОСЛЕ отмены тред остаётся читаемым: следующий запуск поднимает его без
    ошибки и снимает протухший флаг ``summarizing``.
    """
    cfg = compacting_config(tmp_path, monkeypatch)

    from gc_core import run_summary_coordinator as rsc

    # Подменяем имя, связанное У КООРДИНАТОРА: ``from ... import`` связывает его
    # на импорте, и подмена в модуле-источнике не подействует.
    real = rsc.arun_summary_compaction
    entered = asyncio.Event()

    async def slow_compaction(*args, **kwargs):
        entered.set()
        await asyncio.sleep(5)          # отменят раньше, чем досчитает
        return await real(*args, **kwargs)

    monkeypatch.setattr(rsc, "arun_summary_compaction", slow_compaction)
    install(monkeypatch, script())

    agent = await AsyncAgent.create(cfg)
    try:
        task = asyncio.create_task(agent.run("сделай что-нибудь", thread_id="t-s8"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await task
    finally:
        await agent.aclose()

    # Тред читается, а не падает.
    from gc_core.thread_store import ThreadSession

    session = await ThreadSession.afrom_config(
        config_dict=cfg, system_id="sdk", thread_id="t-s8",
    )
    assert isinstance(session.history_messages, list)

    # Протухший флаг снимается на старте следующего прогона.
    from gc_core.run_summary_coordinator import areset_stale_summarizing_flag

    await areset_stale_summarizing_flag(session)
    runtime = session.meta.get("runtime") or {}
    assert not runtime.get("summarizing"), (
        "флаг summarizing остался взведённым после отменённой компакции — "
        "TUI будет вечно показывать «сжимаю контекст»"
    )
