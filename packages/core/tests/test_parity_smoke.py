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


PROMPT = "сколько будет 2+2"


def _run_once(tmp_path, monkeypatch, *, mode: str = "sync"):
    """Один прогон сценария. Возвращает ``(ответ, дамп треда, бэкенд)``.

    ``mode="async"`` — через ``arun`` на отдельном loop; ``"sync"`` — через
    штатный блокирующий вход.
    """
    cfg = parity_config(tmp_path, monkeypatch)
    backend = install(monkeypatch, ScriptedBackend(list(TURNS)))
    ctx = create_run_context(cfg)
    executor = AgentExecutor(ctx)
    if mode == "async":
        answer = asyncio.run(executor.arun([PROMPT], thread_id="t1"))
    else:
        answer = executor.run([PROMPT], thread_id="t1")
    return answer, run_thread_dump(cfg, "cli", "t1"), backend


def test_two_identical_runs_produce_identical_threads(tmp_path, monkeypatch):
    """Прогон воспроизводим: тот же ответ и тот же дамп треда."""
    first_answer, first_dump, _ = _run_once(tmp_path / "run0", monkeypatch)
    second_answer, second_dump, _ = _run_once(tmp_path / "run1", monkeypatch)

    assert first_answer == second_answer
    assert first_dump == second_dump


def test_async_run_matches_sync_run(tmp_path, monkeypatch):
    """ГЕЙТ ПЕРЕЕЗДА: arun даёт то же, что run.

    Сравнивается не только ответ, но и дамп треда: порядок записей, их состав
    и содержимое. Расхождение здесь означает, что асинхронный путь ведёт себя
    иначе, чем тот, на который опираются CLI, TUI и сервер.
    """
    sync_answer, sync_dump, sync_backend = _run_once(tmp_path / "sync", monkeypatch)
    async_answer, async_dump, async_backend = _run_once(
        tmp_path / "async", monkeypatch, mode="async"
    )

    assert async_answer == sync_answer
    assert async_dump == sync_dump
    assert len(async_backend.calls) == len(sync_backend.calls)
    async_backend.assert_clean()


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


def test_async_run_matches_pre_migration_reference(tmp_path, monkeypatch):
    """НАСТОЯЩИЙ ГЕЙТ ПЕРЕЕЗДА: сверка с прогоном на ДО-async коде.

    ``test_async_run_matches_sync_run`` выше тавтологичен по построению:
    ``AgentExecutor.run`` — это ``get_portal().run(self.arun(...))``, то есть
    обе стороны сравнения исполняют ОДНУ корутину. Он ловит зависимость
    поведения от выбора loop, но расхождение с тем, как ядро вело себя ДО
    переезда, поймать не может — а именно это план объявлял целью Задачи 1.

    Эталон снят на ``bf1cb021`` — ``develop`` на момент вливания пакетов
    (08.09.2026). До этого базой был ``c96f26ff`` (до-async рантайм), и пока
    ветка шла рядом, он и был точкой отсчёта.

    Смена базы — не протухание, а смена вопроса. develop намеренно поменял
    наблюдаемое поведение: PR #92 добавил в ответ финализатора поле ``task``, и
    дамп разошёлся бы у любого прогона. Сверяться с эталоном, который этого поля
    не знает, значит проверять, что мы не приняли чужую фичу.

    Инвариант теперь: асинхронный прогон наблюдаемо неотличим от синхронного НА
    ТОМ ЖЕ коде. Пересчитывать эталон на нашей ветке по-прежнему нельзя — он
    станет отражением того, что мы сами и произвели; снимается он прогоном этого
    же харнесса в worktree на develop (подробности — в docstring
    ``test_toggle_matrix``).
    """
    import json
    from pathlib import Path

    reference = json.loads(
        (Path(__file__).parent / "fixtures" / "parity_reference_bf1cb021.json")
        .read_text(encoding="utf-8")
    )

    answer, dump, backend = _run_once(tmp_path, monkeypatch, mode="async")

    assert answer == reference["answer"], "ответ разошёлся с до-async прогоном"
    assert dump == reference["dump"], "дамп треда разошёлся с до-async прогоном"
    backend.assert_clean()
