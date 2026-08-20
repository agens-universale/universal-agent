"""Гейт переезда на async: два прогона одного сценария обязаны совпасть.

Сегодня сравниваются два СИНХРОННЫХ прогона — тавтология, и это намеренно:
харнесс должен существовать до первой async-строки, иначе доказывать
эквивалентность будет нечем. В Задаче 16 (`AgentExecutor.arun`) вторым
аргументом сравнения встанет асинхронный прогон, и тест станет настоящим
гейтом — без правок в этом файле, кроме добавления второго сценария.

См. docs/plans/2026-08-20-async-core.md, Задача 1.
"""
from gc_core.agent_executor import AgentExecutor
from gc_core.run_context import create_run_context

from parity_harness import (
    ScriptedBackend,
    assistant_tool_call,
    judge_accept,
    install,
    parity_config,
    run_thread_dump,
)

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


def _run_once(tmp_path, monkeypatch):
    """Один прогон сценария. Возвращает ``(ответ, дамп треда, бэкенд)``."""
    cfg = parity_config(tmp_path, monkeypatch)
    backend = install(monkeypatch, ScriptedBackend(list(TURNS)))
    ctx = create_run_context(cfg)
    answer = AgentExecutor(ctx).run(["сколько будет 2+2"], thread_id="t1")
    return answer, run_thread_dump(cfg, "cli", "t1"), backend


def test_two_identical_runs_produce_identical_threads(tmp_path, monkeypatch):
    """Прогон воспроизводим: тот же ответ и тот же дамп треда."""
    first_answer, first_dump, _ = _run_once(tmp_path / "run0", monkeypatch)
    second_answer, second_dump, _ = _run_once(tmp_path / "run1", monkeypatch)

    assert first_answer == second_answer
    assert first_dump == second_dump


def test_thread_dump_records_the_whole_turn(tmp_path, monkeypatch):
    """Страховка от «сравнили две пустоты».

    Без неё тест выше зеленеет, даже если прогон ничего не записал, — и гейт
    переезда молча перестаёт что-либо проверять. Заодно фиксирует форму записи:
    ``{"type": "message", "seq": N, "payload": {...}}``.
    """
    _answer, dump, _backend = _run_once(tmp_path, monkeypatch)

    assert dump, "тред пуст — сравнивать нечего"
    assert [rec["seq"] for rec in dump] == list(range(1, len(dump) + 1))

    payloads = [rec["payload"] for rec in dump]
    assert payloads[0] == {"role": "user", "content": "сколько будет 2+2"}

    kinds = [p.get("type") or p.get("role") for p in payloads]
    assert "function_call" in kinds
    assert "function_call_output" in kinds
    names = [p.get("name") for p in payloads if p.get("name")]
    assert names == ["exec_calculator", "turn_finish"], names


def test_scenario_is_consumed_exactly(tmp_path, monkeypatch):
    """Сценарий исполняется ровно на свою длину.

    Лишний поход к модели (зацикливание, повторный вердикт) сделал бы
    сравнение бессмысленным, а недобор означал бы, что ход не дошёл до конца.
    """
    _answer, _dump, backend = _run_once(tmp_path, monkeypatch)

    backend.assert_clean()
    assert len(backend.calls) == len(TURNS)
