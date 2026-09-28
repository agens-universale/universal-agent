"""Завершённый прогон сервера отдаёт логгер своего контекста.

``acreate_run_context`` заводит на ран логгер ``agent_run_<kind>_<run_id>`` с
открытым файлом, а ``logging`` держит его в ``loggerDict`` до конца процесса.
Сервер ведёт прогоны без счёта, и без освобождения каждый запрос оставлял по
логгеру и дескриптору.
"""

import logging
from types import SimpleNamespace

import pytest
from gc_core.utils.logging_module import create_run_logger
from gc_server.rest_api.agent_execution_registry import AgentRunRegistry


class _Executor:
    def __init__(self, ctx):
        self._ctx = ctx

    async def arun(self, *args, **kwargs):
        return "ok"


class _FailingExecutor(_Executor):
    async def arun(self, *args, **kwargs):
        raise RuntimeError("backend down")


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_cls", [_Executor, _FailingExecutor])
async def test_finished_run_releases_its_logger(tmp_path, executor_cls):
    logger, _, _ = create_run_logger(
        {"logs_dump_dir": str(tmp_path), "save_logs": True},
        run_id=f"srv-{executor_cls.__name__}",
    )
    registry = AgentRunRegistry()
    run = await registry.astart_if_absent(
        system="s",
        task_id="t",
        executor_factory=executor_cls,
        ctx=SimpleNamespace(logger=logger),
        initial_user_message="hi",
    )
    await run.thread

    assert logger.name not in logging.Logger.manager.loggerDict
    assert logger.handlers == []
