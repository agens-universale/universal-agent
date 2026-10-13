import pytest
from gc_core.context_preparation import settings_from_snapshot
from gc_core.model_router import ModelRouter
from gc_testkit.settings import fake_ctx, make_settings
from gc_core_temporalio.model_routing import (
    materialize_router_identity,
    public_router_identity,
)
from gc_core_temporalio.public_config import public_config_snapshot


def context():
    loaded = make_settings(
        {
            "models": {
                "active": {
                    "model": "good",
                    "url": "https://active.example/v1",
                    "token": "secret-active",
                },
                "pin": {
                    "model": "dead",
                    "url": "https://pin.example/v1",
                    "token": "secret-pin",
                },
            },
            "slots": {"default": "active"},
            "assign": {"subagents": {"subagent_worker": "pin"}},
        }
    )
    parent = ModelRouter(loaded, level_provider=lambda: "default")
    return fake_ctx(
        loaded=loaded,
        model_router=parent.with_override(
            parent.resolve("subagent:subagent_worker")
        ),
    )


def test_current_child_fallback_identity_materializes_only_worker_transport():
    ctx = context()
    public = public_config_snapshot(ctx)
    parent = ModelRouter(
        settings_from_snapshot(public.settings, public.runtime),
        level_provider=lambda: "default",
    )
    child = parent.with_override(parent.resolve("subagent:subagent_worker"))
    child = child.with_override(parent.resolve("main"))
    identity = public_router_identity(child)
    assert "https://" not in str(identity)
    assert "secret" not in str(identity)
    router = materialize_router_identity(identity, ctx)
    resolved = router.resolve("main")
    assert (
        resolved.alias,
        resolved.model,
        resolved.url,
        resolved.authorization,
    ) == (
        "active",
        "good",
        "https://active.example/v1",
        # Токен как настроен: форму (с Bearer или без) выбирает проба/401.
        "secret-active",
    )
    assert ctx.model_router.resolve("main").alias == "pin"


@pytest.mark.parametrize(
    "change",
    [
        {"alias": "unknown"},
        {"model": "invented"},
        {"url": "https://injected.example"},
        {"authorization": "injected"},
    ],
)
def test_routing_identity_cannot_inject_worker_transport(change):
    ctx = context()
    identity = public_router_identity(ctx.model_router)
    identity["unbound_override"].update(change)
    with pytest.raises(ValueError):
        materialize_router_identity(identity, ctx)


@pytest.mark.parametrize("child", [True, False], ids=["bound_child", "main"])
async def test_materialized_identity_keeps_worker_role_through_local_operations(monkeypatch, child):
    import json
    import httpx
    from gc_core.execution.local import LocalOperations
    from gc_core.execution.model import make_model_request
    from gc_core.run_context import acreate_run_context
    from gc_core.states import State
    from gc_core.utils import connect_backend
    from gc_core_temporalio.child_preparation import prepare_child_context
    from gc_core_temporalio.contracts import ActivityCall

    loaded = make_settings({
        "backend.api_mode": "chat_completions",
        "backend.vision": False,
        "logging.save_logs": False,
        "agent.use_local_subagents": True,
        "models": {
            "mid": {"model": "mid-model", "url": "https://mid.example/v1/chat/completions", "token": "private-mid", "summary_trigger_chars": 20000},
            "big": {"model": "big-model", "url": "https://big.example/v1/chat/completions", "token": "private-big", "summary_trigger_chars": 80000},
        },
        "slots": {"default": "mid", "max": "big"},
        "assign.subagents": {"subagent_worker": "big"},
    })
    parent = await acreate_run_context(loaded)
    if child:
        worker = await prepare_child_context(ActivityCall(
            "test", "child", "prepare", child_context={
                "tool_name": "subagent_worker", "frozen_level": "default",
                "force_legacy": False, "circuit_breaker": {},
            },
        ), parent)
    else:
        worker = parent
    assert worker.model_router.is_subagent_child is child
    public = public_config_snapshot(worker)
    restored = settings_from_snapshot(public.settings, public.runtime)
    assert restored.settings.models["big" if child else "mid"].summary_trigger_chars == (80000 if child else 20000)
    identity = public_router_identity(worker.model_router)
    assert set(identity) == {"level", "force_legacy", "unbound_override"}
    assert "https://" not in str(identity)
    assert "private-" not in str(identity)
    worker.model_router = materialize_router_identity(identity, worker)
    assert worker.model_router.resolve_main_at_level("default", check_breaker=False).summary_trigger_chars == (80000 if child else 20000)

    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "r1", "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        })

    transport = httpx.MockTransport(reply)
    monkeypatch.setattr(connect_backend, "get_async_client", lambda **_: httpx.AsyncClient(transport=transport))
    state = State(verbose=False, model="unused", tools_spec=[], tools_descr=[], skills_list=[],
                  max_steps=5, system_prompt_id="v1", use_server_subagents=False)
    state.turn_level = "default"
    state.messages = [{"role": "user", "content": "Do the task."}]
    operation = LocalOperations(worker)
    request = make_model_request(operation, state, state.messages, consumer="main")
    result = await operation.model(request)
    expected = "big" if child else "mid"
    assert str(requests[0].url) == f"https://{expected}.example/v1/chat/completions"
    assert json.loads(requests[0].content)["model"] == f"{expected}-model"
    assert len(requests) == 1
    assert result.messages


@pytest.mark.parametrize("effective", ["light", "default", "max"])
@pytest.mark.parametrize("mode,bound", [("auto", False), ("default", False), ("auto", True)])
async def test_prepared_child_preserves_frozen_raw_mode(tmp_path, effective, mode, bound):
    """Worker восстанавливает исходный режим отдельно от выбранного уровня родителя."""
    from gc_core.run_context import acreate_run_context
    from gc_core.states import State
    from gc_core_temporalio.child_preparation import prepare_child_context
    from gc_core_temporalio.contracts import ActivityCall
    loaded = make_settings({
        "backend.api_mode": "chat_completions", "backend.vision": False,
        "logging.save_logs": False, "agent.use_local_subagents": True,
        "level": mode,
        "models": {"fast": {}, "mid": {}, "big": {}},
        "slots": {"light": "fast", "default": "mid", "max": "big"},
        "assign.subagents": {"subagent_worker": "big"} if bound else {},
    })
    parent = await acreate_run_context(loaded)
    live = {"value": mode}
    parent.model_router = parent.model_router.with_level_providers(lambda: effective, lambda: live["value"])
    ref = {"tool_name": "subagent_worker", "frozen_level": effective,
           "frozen_live_level": mode, "force_legacy": False, "circuit_breaker": {}}
    child = await prepare_child_context(ActivityCall("test", "child", "prepare", child_context=ref), parent)
    state = State(verbose=False, model="m", tools_spec=child.tools_spec,
                  tools_descr=child.tools_description, skills_list=[], max_steps=5,
                  system_prompt_id="v1", use_server_subagents=False)
    child.attach_state(state)
    live["value"] = "max"  # уже запущенный ребёнок не следует новой команде родителя
    state.current_level = "light"
    first = child.model_router.resolve("main").alias
    state.current_level = "max"
    second = child.model_router.resolve("main").alias

    if bound:
        assert (first, second) == ("big", "big")
        assert child.model_router.auto_allowed is False
    elif mode == "auto":
        assert child.model_router.live_level() == "auto"
        assert (first, second) == ("fast", "big")
    else:
        assert child.model_router.live_level() == "default"
        assert (first, second) == ("mid", "mid")


async def test_old_child_context_without_raw_mode_keeps_effective_level():
    """У старого входа без raw-mode сохраняется прежний ручной уровень."""
    from gc_core.run_context import acreate_run_context
    from gc_core_temporalio.child_preparation import prepare_child_context
    from gc_core_temporalio.contracts import ActivityCall
    parent = await acreate_run_context(make_settings({
        "backend.api_mode": "chat_completions", "logging.save_logs": False,
        "agent.use_local_subagents": True, "level": "auto",
        "models": {"fast": {}, "mid": {}}, "slots": {"light": "fast", "default": "mid"},
    }))
    child = await prepare_child_context(ActivityCall("test", "child", "prepare", child_context={
        "tool_name": "subagent_worker", "frozen_level": "light",
        "force_legacy": False, "circuit_breaker": {},
    }), parent)
    assert child.model_router.live_level() == "light"
    assert child.model_router.resolve("main").alias == "fast"


@pytest.mark.parametrize("difference", ["url", "token", "headers"])
def test_public_child_facts_preserve_working_pairs_and_floor(difference):
    from types import SimpleNamespace
    from gc_core.auto_routing.levels import auto_available, resolve_routing_params
    from gc_core.auto_routing.policy import RoutingParams
    from gc_core.auto_routing.request_equivalence import prepare_request_equivalence
    from gc_core.context_preparation import settings_from_snapshot
    from gc_core.processing_responses import _floor_target
    from gc_core_temporalio.public_config import public_config_snapshot
    from gc_testkit.settings import make_settings

    models = {}
    for alias in ("fast", "mid", "big"):
        models[alias] = {"model": "shared-model", "url": "https://shared.invalid/v1", "token": "common"}
        if difference == "url":
            models[alias]["url"] = f"https://{alias}.invalid/v1"
        elif difference == "token":
            models[alias]["token"] = f"secret-{alias}"
        else:
            models[alias]["backend"] = {"chat_completion_headers": {"X-Private": alias}}
    full = make_settings({"models": models, "level": "auto", "backend.api_mode": "chat_completions",
                          "slots": {"light": "fast", "default": "mid", "max": "big"}})
    snapshot = public_config_snapshot(full)
    restored = settings_from_snapshot(snapshot.settings, snapshot.runtime)
    ctx = SimpleNamespace(settings=restored.settings, runtime=restored.runtime, run_id="child",
        routing_contract_v2=True, prepared_request_equivalence=prepare_request_equivalence(full, context_id="child"))
    ctx.model_router = ModelRouter(ctx, level_provider=lambda: "light")
    assert auto_available(ctx, force_legacy=False) is True
    assert resolve_routing_params(ctx).safe_level == "auto"
    weak = ctx.model_router.resolve("main", level_override="light")
    assert _floor_target(ctx, "light", weak, RoutingParams(safe_level="default")) == "default"


def test_prepared_child_rejects_missing_facts_on_new_version():
    from gc_core.execution.contracts import ExecutionFailure
    from gc_core_temporalio.child_contracts import PreparedChild
    from gc_core_temporalio.child_preparation import PreparedChildContext
    with pytest.raises(ExecutionFailure):
        PreparedChildContext(PreparedChild(None, None), None, independent_auto=True)


def test_old_prepared_child_without_facts_keeps_old_contract():
    from gc_core_temporalio.child_contracts import PreparedChild
    from gc_core_temporalio.child_preparation import PreparedChildContext
    adapter = PreparedChildContext(PreparedChild(None, None), None, independent_auto=False)
    assert adapter.independent_auto is False
