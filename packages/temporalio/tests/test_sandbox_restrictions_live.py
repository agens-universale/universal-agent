"""Песочница воркера: сетевой клиент в коде workflow — отказ, а не запрос.

Модель, инструменты и MCP ходят только из activities; клиент в коде
workflow — признак ввода-вывода ядра мимо порта.
"""

import shutil
import uuid

import pytest
from live_support import temporal_cli
from gc_core_temporalio import sandbox_passthrough  # noqa: F401
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    import httpx
    from gc_core.utils import http_session
    from mcp.client.session import ClientSession


@workflow.defn
class OpensClients:
    @workflow.run
    async def run(self) -> list[str]:
        outcome = []
        for make in (httpx.AsyncClient, http_session._CompatClient):
            try:
                make()
                outcome.append("created")
            except Exception as exc:  # noqa: BLE001
                outcome.append(type(exc).__name__)
        try:
            ClientSession(None, None)
            outcome.append("created")
        except Exception as exc:  # noqa: BLE001
            outcome.append(type(exc).__name__)
        try:
            http_session.NO_NETRC_SESSION.get("http://127.0.0.1:9")
            outcome.append("sent")
        except Exception as exc:  # noqa: BLE001
            outcome.append(type(exc).__name__)
        # Ссылки на типы (isinstance, разбор ошибок) не запрещены.
        outcome.append(str(isinstance(None, httpx.HTTPStatusError)))
        return outcome


async def test_workflow_code_cannot_create_network_clients():
    from gc_core_temporalio.worker import workflow_runner

    binary = temporal_cli()
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary
    ) as env:
        async with Worker(
            env.client,
            task_queue="sandbox",
            workflows=[OpensClients],
            workflow_runner=workflow_runner(),
        ):
            result = await env.client.execute_workflow(
                OpensClients.run,
                id=f"sandbox-{uuid.uuid4().hex}",
                task_queue="sandbox",
            )
    assert result == ["RestrictedWorkflowAccessError"] * 4 + ["False"]
