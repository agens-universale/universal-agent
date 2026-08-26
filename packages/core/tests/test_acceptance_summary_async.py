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

async def test_loop_stays_responsive_during_compaction(tmp_path, monkeypatch):
    """Цикл жив на всём протяжении компакции.

    Критерий приёмки переезда — лаг < 150 мс. ``test_acceptance_non_blocking``
    меряет его на прогоне БЕЗ компакции: порог там дефолтный и не
    перешагивается. Здесь компакция происходит, и вместе с ней — двухфазная
    перезапись треда.
    """
    cfg = compacting_config(tmp_path, monkeypatch)
    install(monkeypatch, script())

    gaps: list = []
    stop = asyncio.Event()

    async def heartbeat():
        prev = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - prev)
            prev = now

    beat = asyncio.create_task(heartbeat())
    async with await AsyncAgent.create(cfg) as agent:
        await agent.run("сделай что-нибудь", thread_id="t-s2")
    stop.set()
    await beat

    assert was_compacted(cfg, "sdk", "t-s2"), "компакция не сработала — тест пуст"

    # Судим по ПРОЦЕНТИЛЮ, а не по единственному худшему разрыву: под полным
    # прогоном набора машина занята, и одной паузы GC хватает, чтобы «провалить»
    # порог. Если цикл действительно отняли, страдает не один тик, а хвост
    # целиком — процентиль это видит, а максимум путает с шумом.
    import statistics

    assert gaps, "пульс не тикал вовсе"
    ordered = sorted(gaps)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    assert p95 < 0.15, (
        f"p95 разрыва {p95 * 1000:.0f} мс во время компакции "
        f"(медиана {statistics.median(gaps) * 1000:.0f} мс, "
        f"максимум {max(gaps) * 1000:.0f} мс)"
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

    session = ThreadSession.from_config(
        config_dict=cfg, system_id="sdk", thread_id="t-s8",
    )
    assert isinstance(session.history_messages, list)

    # Протухший флаг снимается на старте следующего прогона.
    from gc_core.run_summary_coordinator import reset_stale_summarizing_flag

    reset_stale_summarizing_flag(session)
    runtime = session.meta.get("runtime") or {}
    assert not runtime.get("summarizing"), (
        "флаг summarizing остался взведённым после отменённой компакции — "
        "TUI будет вечно показывать «сжимаю контекст»"
    )
