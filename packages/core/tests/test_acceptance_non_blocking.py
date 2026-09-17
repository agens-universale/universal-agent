"""Приёмка переезда (план async-переезда, Задача 26).

Проверяется не «работает ли ядро» — это доказали 3400+ тестов выше, — а ровно
цель переезда: event loop встраивающего приложения остаётся живым, пока агент
ходит в сеть и исполняет тулы.
"""
import asyncio
import pytest
import json
import os
import subprocess
import sys
import time

from gc_core.aio import AsyncAgent
from gc_testkit.paths import package_src_dirs

from parity_harness import (
    ScriptedBackend,
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


#: Ход 1 — агент зовёт exec_sleep (настоящий subprocess, 0.3с).
#: Ход 2 — финализатор. Ход 3 — вердикт судьи finish-gate.
TURNS = [
    assistant_tool_call("exec_sleep", {"seconds": 0.3}, call_id="c1"),
    assistant_tool_call("turn_finish", {
        "solution_type": "direct_answer", "task_report": "поспал", "handoff": "{}",
    }, call_id="c2"),
    judge_accept(),
]


async def _heartbeat(stop: asyncio.Event, gaps: list) -> None:
    """Тикает каждые 10 мс и записывает фактические промежутки."""
    prev = time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(0.01)
        now = time.monotonic()
        gaps.append(now - prev)
        prev = now


async def test_event_loop_stays_responsive_during_run(tmp_path, monkeypatch):
    """Критерий 1: ни сеть, ни subprocess не залипают на loop."""
    cfg = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(list(TURNS)))

    gaps: list = []
    stop = asyncio.Event()
    beat = asyncio.create_task(_heartbeat(stop, gaps))

    async with await AsyncAgent.create(cfg) as agent:
        answer = await agent.run("поспи 0.3 секунды", thread_id="t1")

    stop.set()
    await beat

    assert answer
    assert len(gaps) > 10, f"хартбит почти не тикал: {len(gaps)} тиков"
    from gc_testkit import assert_loop_was_free

    assert_loop_was_free(gaps, tick=0.01)


async def test_two_agents_run_concurrently(tmp_path, monkeypatch):
    """Критерий 2: два прогона идут внахлёст, а не друг за другом."""
    cfg_solo = parity_config(tmp_path / "solo", monkeypatch, model="solo")
    install_many(monkeypatch, {"solo": ScriptedBackend(list(TURNS))})
    async with await AsyncAgent.create(cfg_solo) as solo:
        started = time.monotonic()
        await solo.run("поспи", thread_id="t1")
        single = time.monotonic() - started

    # По сценарию на агента: общий раздаёт ходы в порядке вызовов, и второй
    # агент забрал бы финализатор первого — тест мерил бы очередь, а не
    # параллельность.
    cfg_a = parity_config(tmp_path / "a", monkeypatch, model="агент-а")
    cfg_b = parity_config(tmp_path / "b", monkeypatch, model="агент-б")
    install_many(monkeypatch, {
        "агент-а": ScriptedBackend(list(TURNS)),
        "агент-б": ScriptedBackend(list(TURNS)),
    })
    a = await AsyncAgent.create(cfg_a)
    b = await AsyncAgent.create(cfg_b)
    started = time.monotonic()
    async with a, b:
        r1, r2 = await asyncio.gather(
            a.run("поспи", thread_id="t1"), b.run("поспи", thread_id="t2")
        )
    pair = time.monotonic() - started

    assert r1 and r2
    assert pair < single * 1.6, (
        f"два агента заняли {pair:.2f}с при одиночном {single:.2f}с — "
        f"похоже, шли последовательно"
    )


def test_agent_runs_without_importing_frontends(tmp_path):
    """Критерий 3: ядро самодостаточно — фронтенды не подтягиваются.

    Отдельный интерпретатор, потому что в общем прогоне gc_tui мог быть
    импортирован соседним тестом.
    """
    script = """
import asyncio, json, sys
from gc_core.aio import AsyncAgent

async def main():
    agent = await AsyncAgent.create({
        "backend_api_mode": "chat_completions",
        "completions_url": "http://acceptance.invalid/v1/chat/completions",
        "model": "acceptance-model",
        "threads_data_path": %(threads)r,
        "logs_dump_dir": %(logs)r,
        "save_logs": False,
        "launch_root": %(root)r,
        "backend_request_timeout_sec": 60,
        "max_steps": 1,
    })
    await agent.aclose()

try:
    asyncio.run(main())
except Exception:
    pass    # цель теста — импорты, а не успешность сборки контекста
leaked = [m for m in ("gc_cli", "gc_tui", "gc_server") if m in sys.modules]
print(json.dumps(leaked))
""" % {
        "threads": str(tmp_path / "threads"),
        "logs": str(tmp_path / "logs"),
        "root": str(tmp_path),
    }

    # PYTHONPATH исходников: без него дочерний процесс берёт УСТАНОВЛЕННУЮ копию
    # gc_core, а не ту, которую гоняет набор, — и меряет чужое дерево. Здесь это
    # видно сразу (`No module named 'gc_core.aio'`), но в обратную сторону —
    # когда установленная копия свежее — тест молча зеленел бы не на том коде.
    env = dict(os.environ)
    src_dirs = [str(d) for d in package_src_dirs()]
    if src_dirs:
        env["PYTHONPATH"] = os.pathsep.join(
            src_dirs + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )

    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []
