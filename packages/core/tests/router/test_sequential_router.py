"""Sequential routing never allocates thread locks; worker routing still does."""

from types import SimpleNamespace

import pytest
from gc_core import model_router
from gc_core.model_router import (
    CircuitBreaker,
    ModelRouter,
    consumer_has_transport,
)
from gc_core.auto_routing.request_equivalence import resolve_level_requests
from gc_testkit.settings import make_settings


def settings():
    return make_settings(
        {
            "backend.completions_url": "",
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


@pytest.mark.parametrize("restored", [False, True])
def test_factory_helpers_and_clones_never_create_thread_locks(
    monkeypatch, restored
):
    def forbidden_lock():
        raise AssertionError("thread lock inside sequential routing")

    monkeypatch.setattr(
        model_router, "threading", SimpleNamespace(Lock=forbidden_lock)
    )
    snapshot = {"m": "offline"} if restored else None
    router = ModelRouter.sequential(
        settings(),
        choice_provider=lambda: "default",
        live_choice_provider=lambda: "auto",
        breaker_snapshot=snapshot,
    )
    assert router.live_choice() == "auto"
    assert router.breaker.snapshot() == (snapshot or {})
    if snapshot is not None:
        snapshot.clear()
        assert router.breaker.is_open("m")
    resolved = router.resolve("main", check_breaker=False)
    for clone in (
        router.with_override(resolved),
        router.with_choice_providers(lambda: "max", lambda: "max"),
    ):
        assert clone.breaker is router.breaker
        assert clone.resolve("main", check_breaker=False).model == "model"
    child = router.breaker.copy()
    child.trip("child", "offline")
    assert not router.breaker.is_open("child")
    assert consumer_has_transport(settings(), "main")
    assert resolve_level_requests(settings())


def test_worker_constructor_and_restore_keep_real_locks(monkeypatch):
    lock_factory = model_router.threading.Lock
    locks = []

    def tracked_lock():
        lock = lock_factory()
        locks.append(lock)
        return lock

    monkeypatch.setattr(
        model_router, "threading", SimpleNamespace(Lock=tracked_lock)
    )
    router = ModelRouter(settings(), choice_provider=lambda: "default")
    restored = CircuitBreaker.restore({"m": "offline"})
    copied = restored.copy()
    assert len(locks) == 3
    for breaker, lock in zip((router.breaker, restored, copied), locks):
        assert breaker._lock is lock
        breaker.trip("other", "offline")
        assert not lock.locked()
