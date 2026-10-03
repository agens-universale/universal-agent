import pytest
from gc_core.context_preparation import settings_from_snapshot
from gc_core.model_router import ModelRouter
from gc_testkit.settings import fake_ctx, make_settings
from gc_ua_temporalio.native_model_routing import (
    materialize_router_identity,
    public_router_identity,
)
from gc_ua_temporalio.public_config import public_config_snapshot


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
        "Bearer secret-active",
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
