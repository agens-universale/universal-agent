"""Адаптер Harbor: пул закрывается, отмена не теряется, дескриптор под локом.

Три дефекта одного класса — трайл кончился, а ресурс живёт:

* ``_agent_run`` не закрывал пул httpx своего ``asyncio.run``. Реестр
  клиентов ключуется по loop, loop умирает вместе с прогоном, и клиент трайла
  доживал до сборщика — со всплывающим «Event loop is closed» в чужом месте;
* ``executor`` присваивался ПОСЛЕ ``acreate_run_context``. Отмена во время
  дискавери (до 120 с) заставала ``executor is None``: ``stop_run()`` звать
  было не на чем, и поток спокойно уходил в ``arun`` уже после отмены;
* ``StepTimings._emit`` читал ``self._fh`` вне лока, а писал внутри. Между
  чтением и локом успевал ``close()``, и запись уходила в закрытый файл.
"""

import ast
import sys
import threading
from pathlib import Path

from gc_testkit.paths import HARBOR

ADAPTER = HARBOR / "adapters" / "gc_cli_external.py"


def _adapter_tree():
    return ast.parse(ADAPTER.read_text(encoding="utf-8"))


def _func(tree, name):
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    raise AssertionError(f"{name} не найдена в адаптере")


def test_agent_run_closes_its_pool_in_finally():
    """Пул закрывается именно в `finally`: иначе отказ прогона его пропустит."""
    run = _func(_adapter_tree(), "_agent_run")

    tries = [n for n in ast.walk(run) if isinstance(n, ast.Try)]
    assert tries, "_agent_run без try/finally — пул не закроется на отказе"
    closed_in_finally = any(
        isinstance(c.func, ast.Name) and c.func.id == "aclose_clients"
        for t in tries
        for stmt in t.finalbody
        for c in ast.walk(stmt)
        if isinstance(c, ast.Call)
    )
    assert closed_in_finally, (
        "aclose_clients() не в finally — клиент трайла доживёт до сборщика"
    )


def test_agent_run_releases_the_trial_logger_in_finally():
    """Логгер трайла отдаётся в `finally`: Harbor ведёт трайлы без счёта в
    одном процессе, а `logging` держит каждый логгер с открытым файлом до конца."""
    run = _func(_adapter_tree(), "_agent_run")

    tries = [n for n in ast.walk(run) if isinstance(n, ast.Try)]
    released_in_finally = any(
        isinstance(c.func, ast.Name) and c.func.id == "release_run_logger"
        for t in tries
        for stmt in t.finalbody
        for c in ast.walk(stmt)
        if isinstance(c, ast.Call)
    )
    assert released_in_finally, (
        "release_run_logger() не в finally — логгер трайла живёт до конца процесса"
    )


def test_cancellation_is_recorded_before_the_executor_exists():
    """Флаг отмены обязан читаться ПЕРЕД `arun`, а ставиться в обработчике."""
    tree = _adapter_tree()
    run = _func(tree, "_agent_run")

    names = {n.attr for n in ast.walk(run) if isinstance(n, ast.Attribute)}
    assert "_cancelled" in names, (
        "_agent_run не смотрит на отмену: отменённый трайл всё равно уйдёт в arun"
    )
    src = ADAPTER.read_text(encoding="utf-8")
    assert "self._cancelled = True" in src, "флаг отмены никто не ставит"
    assert "_cancel_lock" in src, (
        "флаг без лока: ставит его loop Harbor, читает рабочий поток агента"
    )


def test_step_timings_reads_the_handle_under_the_lock():
    """Гонка на закрытии: дескриптор и читается, и пишется под одним локом."""
    sys.path.insert(0, str(HARBOR))
    for key in [
        k for k in sys.modules if k.startswith("adapters.step_timings")
    ]:
        del sys.modules[key]
    import adapters.step_timings as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    emit = _func(ast.parse(src), "_emit")

    # Первый оператор тела — вход в лок; чтение дескриптора уже внутри.
    assert isinstance(emit.body[0], ast.With), (
        "_emit снова читает self._fh до входа в лок"
    )
    read_lines = [
        n.lineno
        for n in ast.walk(emit)
        if isinstance(n, ast.Attribute) and n.attr == "_fh"
    ]
    assert read_lines and min(read_lines) > emit.body[0].lineno, read_lines


def test_step_timings_survives_a_close_during_writes(tmp_path):
    """Свойство, а не форма: закрытие под нагрузкой не должно ронять писателя."""
    sys.path.insert(0, str(HARBOR))
    import adapters.step_timings as mod

    timings = mod.StepTimings(tmp_path / "t.jsonl")
    timings.attach()
    errors: list = []
    stop = threading.Event()

    def _writer():
        while not stop.is_set():
            try:
                timings._emit({"k": "v"})
            except BaseException as exc:  # нам важен сам факт
                errors.append(exc)
                return

    threads = [threading.Thread(target=_writer) for _ in range(4)]
    for t in threads:
        t.start()
    for _ in range(50):
        timings.close()
        timings.attach()
    stop.set()
    for t in threads:
        t.join(10)
    timings.close()

    assert not errors, f"гонка на дескрипторе: {errors[:3]}"
