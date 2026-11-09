"""Предохранитель workflow после ответа activity модели (спека
model-breaker §2.2, durable): состояние берётся из ответа целиком —
открытые из снимка, права на пробу не остаются."""

import asyncio
from types import SimpleNamespace

from gc_core.model_router import ModelRouter
from gc_core_temporalio.operations import WorkflowOperations
from gc_testkit.settings import make_settings


def _router() -> ModelRouter:
    settings = make_settings(
        {"models": {"mid": {}, "big": {}}, "slots": {"default": "mid"}}
    )
    return ModelRouter(settings, choice_provider=lambda: "default")


def test_reply_breaker_replaces_workflow_state_without_probe_rights():
    operations = WorkflowOperations(None, "owner", lambda prefix: prefix)
    router = _router()
    router.breaker.trip("mid", "ReadError")
    router.breaker.allow_probe()
    operations.router = router

    async def reply(_request, _router):
        return SimpleNamespace(result="ok", circuit_breaker={"big": "503"})

    operations._request_model = reply

    assert asyncio.run(operations.model(object())) == "ok"
    assert router.breaker.known_unavailable("mid") is None
    assert router.breaker.open_reason("big") == "503"
