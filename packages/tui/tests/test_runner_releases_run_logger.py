"""Снятый runner отдаёт логгер своего прогона, и приложение уходит сборщику.

``acreate_run_context`` заводит на ран логгер ``agent_run_<kind>_<run_id>`` с
файлом и stderr-хендлером, а ``logging`` держит его в ``loggerDict`` до конца
процесса. TUI его не отдавал никогда. В тестах stderr — перехват Textual, и
каждый такой логгер держал приложение целиком (экран, runner, executor, кэши
отрисовки): около 0,6 МБ на тест, полный прогон в одном процессе рос до
гигабайт и однажды был убит по нехватке памяти.
"""
import asyncio
import gc
import logging
import threading
import time
import weakref
from types import SimpleNamespace

import pytest

from gc_core.utils.logging_module import create_run_logger
from gc_tui.services.agent_runner import AgentRunner


def _registered(logger) -> bool:
    return logger.name in logging.Logger.manager.loggerDict


def _run_logger(tmp_path, run_id):
    logger, _, _ = create_run_logger(
        {"logs_dump_dir": str(tmp_path), "save_logs": True}, run_id=run_id
    )
    return logger


class _Executor:
    def __init__(self, logger):
        self._ctx = SimpleNamespace(logger=logger)
        self.finish = threading.Event()
        self.running = threading.Event()

    async def arun(self, *args, **kwargs):
        self.running.set()
        await asyncio.to_thread(self.finish.wait, 5.0)

    def stop_run(self):
        self.finish.set()

    def add_message(self, message):
        pass


def test_idle_runner_releases_its_logger(tmp_path):
    logger = _run_logger(tmp_path, "idle")
    runner = AgentRunner(executor=_Executor(logger))
    assert _registered(logger) and logger.handlers

    runner.release()

    assert not _registered(logger)
    assert logger.handlers == []


def test_release_during_a_run_waits_for_the_worker(tmp_path):
    """Стоп посреди HTTP: у пишущего прогона хендлеры не отнимаем."""
    logger = _run_logger(tmp_path, "busy")
    ex = _Executor(logger)
    runner = AgentRunner(executor=ex)
    runner.start(query="x", thread_id="t1")
    assert ex.running.wait(5.0)

    runner.release()
    assert _registered(logger) and logger.handlers, "отняли хендлеры у живого прогона"

    ex.stop_run()
    runner.wait(5.0)
    assert not _registered(logger)
    assert logger.handlers == []


def test_exited_app_is_collected(tmp_path):
    """Сам симптом: после выхода приложение со всем, что при нём, собирается.

    Приложение живёт в СВОЁМ ``asyncio.run``, и проверка идёт после закрытия
    loop — как между тестами. Пока loop жив, его хэндлы законно держат App.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    cfg = {"model": "M", "agent_md_path": str(tmp_path / "agent.md"),
           "threads_data_path": str(tmp_path / "threads"),
           "logs_dump_dir": str(tmp_path / "logs"), "mcp-servers": {}}

    async def _session():
        app = TuiApp(threads_dir=str(tmp_path), system="cli", config=cfg)
        async with app.run_test() as pilot:
            for _ in range(250):
                runner = app.active_runner
                if runner is not None and runner._executor is not None:
                    break
                await pilot.pause(0.02)
            else:
                pytest.fail("executor так и не собрался")
            name = runner._executor._ctx.logger.name
        return weakref.ref(app), name

    ref, name = asyncio.run(_session())

    for _ in range(100):
        if name not in logging.Logger.manager.loggerDict:
            break
        time.sleep(0.02)
    assert name not in logging.Logger.manager.loggerDict, "логгер прогона пережил приложение"

    gc.collect()
    alive = ref() is not None     # не держим App в кадре упавшего assert
    assert not alive, "приложение пережило выход — его что-то держит"
