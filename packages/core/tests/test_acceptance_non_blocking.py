"""Приёмка переезда (план async-переезда, Задача 26).

Проверяется не «работает ли ядро» — это доказали 3400+ тестов выше, — а ровно
цель переезда: event loop встраивающего приложения остаётся живым, пока агент
ходит в сеть и исполняет тулы.
"""

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest
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


#: Ход 1 — агент зовёт exec_sleep (нативный ``asyncio.sleep``, 0.3с).
#: Ход 2 — финализатор. Ход 3 — вердикт судьи finish-gate.
TURNS = [
    assistant_tool_call("exec_sleep", {"seconds": 0.3}, call_id="c1"),
    assistant_tool_call(
        "turn_finish",
        {
            "solution_type": "direct_answer",
            "task_report": "поспал",
            "handoff": "{}",
        },
        call_id="c2",
    ),
    judge_accept(),
]


async def test_event_loop_stays_responsive_during_run(tmp_path, monkeypatch):
    """Критерий 1: loop свободен всё время жизни агента — от сборки до закрытия."""
    # Типизированные настройки собираем ДО окна замера: валидация конфига —
    # работа встраивающего, а не сборки агента.
    settings = parity_config(tmp_path, monkeypatch)
    install(monkeypatch, ScriptedBackend(list(TURNS)))

    from gc_testkit import (
        LoopPulse,
        assert_loop_was_free,
        quiet_gc_for_loop_timing,
    )

    # Сборщик глушится на окне замера: замерено зондом, что в полном наборе в
    # это окно попадает до 872 мс пауз stop-the-world при собственной работе
    # 0,39 с — тест мерил сборщик, а не ядро. В изоляции пауз нет.
    #
    # Пульс прогрет и тикает ДО сборки агента. Прежде задача пульса ставилась
    # в очередь прямо перед `await AsyncAgent.create`, и синхронный кусок
    # сборки до первой уступки в замер не попадал: подменные 0,35 с сна в
    # начале `create` тест не замечал.
    with quiet_gc_for_loop_timing():
        async with LoopPulse() as pulse:
            async with await AsyncAgent.create(settings) as agent:
                answer = await agent.run("поспи 0.3 секунды", thread_id="t1")

    assert answer
    assert len(pulse.gaps) > 10, (
        f"хартбит почти не тикал: {len(pulse.gaps)} тиков"
    )
    # Два пропуска, а не один по умолчанию. У хода два куска без уступки —
    # от начала `run` до первого `await` и хвост после последнего, — и
    # файловое хранилище тредов пишет и читает в них инлайн, как и задумано
    # (docs/async.md). Замерено в изоляции: каждый кусок даёт разрыв 15–31 мс
    # при пороге пропуска 20 мс; с хранилищем в памяти те же куски — 14–17 мс.
    # Прогретый пульс видит оба, и с допуском в один тест краснел в одном
    # изолированном прогоне из десяти. Третий пропуск — уже не дизайн. Одну
    # долгую блокировку судит потолок, и он не ослаблен.
    assert_loop_was_free(pulse.gaps, tick=pulse.step, allowed_stalls=2)


async def test_two_agents_run_concurrently(tmp_path, monkeypatch):
    """Критерий 2: два прогона идут внахлёст, а не друг за другом."""
    from gc_testkit import quiet_gc_for_loop_timing

    cfg_solo = parity_config(tmp_path / "solo", monkeypatch, model="solo")
    install_many(monkeypatch, {"solo": ScriptedBackend(list(TURNS))})
    # Оба замера — под заглушенным сборщиком. Иначе тест сравнивает не
    # параллельность, а то, сколько пауз поколения 2 попало в каждую фазу:
    # замерено 15 пауз на 872 мс в окне пары против одной в окне одиночки, и
    # отношение уезжало к 2× при неперекрываемых 0,09 с собственной работы.
    with quiet_gc_for_loop_timing():
        async with await AsyncAgent.create(cfg_solo) as solo:
            started = time.monotonic()
            await solo.run("поспи", thread_id="t1")
            single = time.monotonic() - started

        # По сценарию на агента: общий раздаёт ходы в порядке вызовов, и второй
        # агент забрал бы финализатор первого — тест мерил бы очередь, а не
        # параллельность.
        cfg_a = parity_config(tmp_path / "a", monkeypatch, model="агент-а")
        cfg_b = parity_config(tmp_path / "b", monkeypatch, model="агент-б")
        install_many(
            monkeypatch,
            {
                "агент-а": ScriptedBackend(list(TURNS)),
                "агент-б": ScriptedBackend(list(TURNS)),
            },
        )
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
        f"два агента заняли {pair:.2f}с при одиночном {single:.2f}с — похоже, шли последовательно"
    )


def test_agent_runs_without_importing_frontends(tmp_path):
    """Критерий 3: ядро самодостаточно — фронтенды не подтягиваются.

    Отдельный интерпретатор, потому что в общем прогоне gc_tui мог быть
    импортирован соседним тестом.
    """
    script = """
import asyncio, json, sys
from gc_core.aio import AsyncAgent
from gc_core.settings import LoadedSettings, RuntimeEnv, default_settings

async def main():
    agent = await AsyncAgent.create(LoadedSettings(
        settings=default_settings(**{{
            "backend.api_mode": "chat_completions",
            "backend.completions_url": "http://acceptance.invalid/v1/chat/completions",
            "agent.model": "acceptance-model",
            "paths.threads_data_path": {threads!r},
            "paths.logs_dump_dir": {logs!r},
            "logging.save_logs": False,
            "backend.request_timeout_sec": 60,
            "agent.max_steps": 1,
        }}),
        runtime=RuntimeEnv(launch_root={root!r}),
    ))
    await agent.aclose()

try:
    asyncio.run(main())
except Exception:
    pass    # цель теста — импорты, а не успешность сборки контекста
leaked = [m for m in ("gc_cli", "gc_tui", "gc_server") if m in sys.modules]
print(json.dumps(leaked))
""".format(
        threads=str(tmp_path / "threads"),
        logs=str(tmp_path / "logs"),
        root=str(tmp_path),
    )

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
