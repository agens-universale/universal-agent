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
from gc_core.config_modules.root import AppConfig
from gc_core.model_router import ModelRouter, consumer_has_transport
from gc_core.auto_routing.request_equivalence import resolve_level_requests

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


@workflow.defn(failure_exception_types=[Exception])
class RoutingFactories:
    @workflow.run
    async def run(self) -> dict:
        settings = AppConfig.model_validate(
            {
                "backend": {"completions_url": ""},
                "models": {
                    "m": {
                        "model": "model",
                        "url": "http://unused/v1",
                        "token": "test",
                    }
                },
                "slots": {"default": "m", "max": "m", "light": "m"},
            }
        )
        unsafe_error = None
        try:
            ModelRouter(settings, choice_provider=lambda: "default")
        except Exception as exc:
            unsafe_error = type(exc).__name__
        router = ModelRouter.sequential(
            settings,
            choice_provider=lambda: "default",
            breaker_snapshot={"m": "offline"},
        )
        restored = router.breaker.snapshot()
        child = router.breaker.copy()
        child.allow_probe()
        probe = child.take_probe("m")
        clone = router.with_choice_providers(lambda: "max", lambda: "max")
        resolved = clone.resolve("main", check_breaker=False)
        return {
            "unsafe_error": unsafe_error,
            "restored": restored,
            "probe": probe,
            "clone_model": router.with_override(resolved)
            .resolve("main", check_breaker=False)
            .model,
            "transport": consumer_has_transport(settings, "main"),
            "levels": sorted(resolve_level_requests(settings)),
        }


async def test_sequential_routing_factory_runs_under_real_workflow_restrictions():
    from gc_core_temporalio.worker import workflow_runner

    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(
            env.client,
            task_queue="routing-locks",
            workflows=[RoutingFactories],
            workflow_runner=workflow_runner(),
        ):
            result = await env.client.execute_workflow(
                RoutingFactories.run,
                id=f"routing-locks-{uuid.uuid4().hex}",
                task_queue="routing-locks",
            )
    assert result["unsafe_error"] == "RestrictedWorkflowAccessError"
    assert result["restored"] == {"m": "offline"}
    assert result["probe"] is True
    assert result["clone_model"] == "model"
    assert result["transport"] is True
    assert len(result["levels"]) == 3
