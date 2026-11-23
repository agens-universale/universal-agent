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
import asyncio
import json
import sys
import threading
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

import pytest
from gc_core import run_context
from gc_core.backends.harbor import HarborBackend
from gc_testkit.paths import HARBOR
from gc_testkit.settings import make_settings

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


async def test_agent_worker_preserves_harbor_phase_context(
    tmp_path, monkeypatch
):
    pytest.importorskip("harbor")
    monkeypatch.syspath_prepend(str(HARBOR))
    from adapters.gc_cli_external import GcCliExternal

    agent = GcCliExternal(logs_dir=tmp_path, model_name="openai/test")
    monkeypatch.setattr(agent, "_build_settings", lambda: None)
    monkeypatch.setattr(
        agent, "_apply_injected_skills", lambda settings, _: settings
    )
    monkeypatch.setattr(agent, "_dump_outcome", lambda *args, **kwargs: None)
    phase_path = ContextVar("phase_path", default=None)
    observed = []

    async def stop_at_context(*_args, **_kwargs):
        observed.append(phase_path.get())
        raise RuntimeError("Stop after context construction")

    monkeypatch.setattr(run_context, "acreate_run_context", stop_at_context)
    token = phase_path.set("/opt/trial-bin:/usr/bin")
    try:
        with pytest.raises(
            RuntimeError, match="Stop after context construction"
        ):
            await agent.run(
                "Test phase environment", SimpleNamespace(), SimpleNamespace()
            )
    finally:
        phase_path.reset(token)

    assert observed == ["/opt/trial-bin:/usr/bin"]


@pytest.mark.parametrize(
    "outcome", ["closed", "worker-active", "cleanup-failed"]
)
async def test_workspace_verification_requires_worker_shutdown(
    outcome, tmp_path, monkeypatch
):
    pytest.importorskip("harbor")
    monkeypatch.syspath_prepend(str(HARBOR))
    from adapters import gc_cli_external
    from gc_core import agent_executor

    worker_started = asyncio.Event()
    allow_worker_exit = threading.Event()
    cleanup_finished = threading.Event()
    trial_loop = asyncio.get_running_loop()
    environment = SimpleNamespace()
    backend = HarborBackend(environment, trial_loop)

    async def release_workspace():
        cleanup_finished.set()
        if outcome == "cleanup-failed":
            raise TimeoutError("Cleanup did not confirm removal")

    class Worker:
        def __init__(self, _ctx):
            pass

        async def arun(self, _instructions):
            trial_loop.call_soon_threadsafe(worker_started.set)
            if not allow_worker_exit.wait(timeout=5):
                raise RuntimeError("Test did not release the agent worker")

        def stop_run(self):
            if outcome != "worker-active":
                allow_worker_exit.set()

        def get_state(self):
            return SimpleNamespace(
                messages=[{"role": "user", "content": "Check shutdown"}]
            )

    async def create_context(*_args, **_kwargs):
        return SimpleNamespace(os_backend=backend)

    async def skip_skills_debug(*_args):
        pass

    agent = gc_cli_external.GcCliExternal(
        logs_dir=tmp_path, model_name="openai/test"
    )
    settings = make_settings({"os_backend": "harbor-workspace"})
    monkeypatch.setattr(agent, "_build_settings", lambda: settings)
    monkeypatch.setattr(agent, "_write_skills_debug", skip_skills_debug)
    monkeypatch.setattr(agent_executor, "AgentExecutor", Worker)
    monkeypatch.setattr(run_context, "acreate_run_context", create_context)
    monkeypatch.setattr(backend, "arelease_workspace", release_workspace)
    monkeypatch.setattr(gc_cli_external, "_STOP_GRACE_S", 0.2, raising=False)
    run = asyncio.create_task(
        agent.run(
            "Check shutdown", environment, gc_cli_external.AgentContext()
        )
    )
    try:
        await asyncio.wait_for(worker_started.wait(), timeout=5)
        run.cancel()
        expected = (
            asyncio.CancelledError if outcome == "closed" else RuntimeError
        )
        with pytest.raises(expected):
            await asyncio.wait_for(run, timeout=5)
        assert cleanup_finished.is_set() == (outcome != "worker-active")
        trace = json.loads(
            (tmp_path / "trajectory.json").read_text(encoding="utf-8")
        )
        assert trace["extra"]["partial"] is True
    finally:
        allow_worker_exit.set()
        assert await asyncio.to_thread(cleanup_finished.wait, 5)


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
