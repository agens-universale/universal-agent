"""Гейт переезда на async: прогон обязан совпасть с до-миграционным.

Гейтов здесь два, и они разные по силе:

* ``test_async_run_matches_sync_run`` сравнивает ``arun`` с ``run``. После
  Задачи 16 это тавтология по построению — ``run`` есть ``portal.run(arun)``,
  обе стороны исполняют одну корутину. Тест не бесполезен (ловит зависимость
  поведения от того, на каком loop идёт прогон), но расхождение с прежним
  ядром поймать не может.
* ``test_async_run_matches_pre_migration_reference`` сравнивает с ЭТАЛОНОМ,
  снятым на до-async коде и закоммиченным в ``fixtures/``. Вот он и есть
  доказательство эквивалентности, которое план объявлял целью Задачи 1.

См. docs/plans/2026-08-20-async-core.md, Задача 1, и
docs/reports/2026-08-21-async-core-audit.md.
"""
import asyncio

from gc_core.agent_executor import AgentExecutor
import pytest
from gc_core.run_context import acreate_run_context

from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    judge_accept,
    install,
    parity_config,
    run_thread_dump,
)


@pytest.fixture(autouse=True)
def _stub_turn_finish(monkeypatch):
    """Подменяем ТОЛЬКО ``turn_finish``: у стенда нет tools_data.

    После правки develop «батч с turn_finish — сначала вся work, закрытие по
    output» ход ИСПОЛНЯЕТ финализатор и закрывается по факту ``ok``. Без
    tools_data это unknown tool, ход не закрывается и тратит лишний
    backend-вызов — сценарий переставал сходиться (`assert_clean`).

    Делегирующая: `exec_calculator` в сценарии обязан отработать по-настоящему,
    иначе дамп треда, который здесь и сверяется, окажется другим.
    """
    from gc_core import processing_responses as pr
    from helpers.turn_finish_apply import stub_turn_finish_apply

    real = pr.aapply_tool

    async def _dispatch(**kwargs):
        if kwargs.get("tool_name") == "turn_finish":
            return await stub_turn_finish_apply(**kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(pr, "aapply_tool", _dispatch)


#: Сценарий: модель зовёт калькулятор, затем закрывает ход финализатором.
#: Ход завершается ТОЛЬКО через turn_finish: голый текст ассистента цикл
#: возвращает агенту (no-tool-call bounce), и сценарий бы не сошёлся.
def _finish(task_report: str, call_id: str = "c9") -> dict:
    return assistant_tool_call("turn_finish", {
        "solution_type": "direct_answer",
        "task_report": task_report,
        "handoff": "{}",
    }, call_id=call_id)


TURNS = [
    assistant_tool_call("exec_calculator", {"expression": "2+2"}, call_id="c1"),
    _finish("2+2 = 4", call_id="c2"),
    # Третий ход — не агента: после финализатора к модели идёт судья
    # finish-gate. Ему нужен сырой JSON-вердикт; вызов тула он считает
    # невалидным ответом и переспрашивает — сценарий бы не сошёлся.
    judge_accept(),
]


PROMPT = "сколько будет 2+2"


async def _run_once(tmp_path, monkeypatch):
    """Один прогон сценария. Возвращает ``(ответ, дамп треда, бэкенд)``.

    Форма у прогона ровно одна: ``arun`` на loop вызывающего. Параметр
    ``mode`` отсюда снят вместе с тавтологией, которую он обслуживал, — см.
    ``test_async_run_matches_pre_migration_reference``.
    """
    cfg = parity_config(tmp_path, monkeypatch)
    backend = install(monkeypatch, ScriptedBackend(list(TURNS)))
    ctx = await acreate_run_context(cfg)
    executor = AgentExecutor(ctx)
    answer = await executor.arun([PROMPT], thread_id="t1")
    return answer, run_thread_dump(cfg, "cli", "t1"), backend


async def test_two_identical_runs_produce_identical_threads(tmp_path, monkeypatch):
    """Прогон воспроизводим: тот же ответ и тот же дамп треда."""
    first_answer, first_dump, _ = await _run_once(tmp_path / "run0", monkeypatch)
    second_answer, second_dump, _ = await _run_once(tmp_path / "run1", monkeypatch)

    assert first_answer == second_answer
    assert first_dump == second_dump


async def test_thread_dump_records_the_whole_turn(tmp_path, monkeypatch):
    """Страховка от «сравнили две пустоты».

    Без неё тест выше зеленеет, даже если прогон ничего не записал, — и гейт
    переезда молча перестаёт что-либо проверять. Заодно фиксирует форму записи:
    ``{"type": "message", "seq": N, "payload": {...}}``.
    """
    _answer, dump, _backend = await _run_once(tmp_path, monkeypatch)

    assert dump, "тред пуст — сравнивать нечего"
    assert [rec["seq"] for rec in dump] == list(range(1, len(dump) + 1))

    payloads = [rec["payload"] for rec in dump]
    assert payloads[0] == {"role": "user", "content": "сколько будет 2+2"}

    kinds = [p.get("type") or p.get("role") for p in payloads]
    assert "function_call" in kinds
    assert "function_call_output" in kinds
    names = [p.get("name") for p in payloads if p.get("name")]
    assert names == ["exec_calculator", "turn_finish"], names


async def test_scenario_is_consumed_exactly(tmp_path, monkeypatch):
    """Сценарий исполняется ровно на свою длину.

    Лишний поход к модели (зацикливание, повторный вердикт) сделал бы
    сравнение бессмысленным, а недобор означал бы, что ход не дошёл до конца.
    """
    _answer, _dump, backend = await _run_once(tmp_path, monkeypatch)

    backend.assert_clean()
    assert len(backend.calls) == len(TURNS)


async def test_async_run_matches_pre_migration_reference(tmp_path, monkeypatch):
    """НАСТОЯЩИЙ ГЕЙТ ПЕРЕЕЗДА: сверка с прогоном на ДО-async коде.

    Здесь стоял ещё и ``test_async_run_matches_sync_run``. Он был тавтологичен
    по построению: ``AgentExecutor.run`` — это ``get_portal().run(self.arun(...))``,
    то есть обе стороны сравнения исполняли ОДНУ корутину; после снятия портала
    и синхронного входа он сравнивал ``arun`` с ``arun`` уже совсем буквально.
    Единственный гейт переезда — этот: сверка с поведением ДО переезда.

    Эталон снят на ``bf1cb021`` — ``develop`` на момент вливания пакетов
    (08.09.2026). До этого базой был ``c96f26ff`` (до-async рантайм), и пока
    ветка шла рядом, он и был точкой отсчёта.

    Смена базы — не протухание, а смена вопроса. develop намеренно поменял
    наблюдаемое поведение: PR #92 добавил в ответ финализатора поле ``task``, и
    дамп разошёлся бы у любого прогона. Сверяться с эталоном, который этого поля
    не знает, значит проверять, что мы не приняли чужую фичу.

    Инвариант: прогон этой ветки наблюдаемо неотличим от прогона develop.
    Пересчитывать эталон на нашей ветке по-прежнему нельзя — он
    станет отражением того, что мы сами и произвели; снимается он прогоном этого
    же харнесса в worktree на develop (подробности — в docstring
    ``test_toggle_matrix``).
    """
    import json
    from pathlib import Path

    reference = json.loads(
        (Path(__file__).parent / "fixtures" / "parity_reference_956a848a.json")
        .read_text(encoding="utf-8")
    )

    answer, dump, backend = await _run_once(tmp_path, monkeypatch)

    assert answer == reference["answer"], "ответ разошёлся с прогоном на develop"
    assert dump == reference["dump"], "дамп треда разошёлся с прогоном на develop"
    backend.assert_clean()
