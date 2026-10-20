"""Уровень модели на сервере — только из конфига (`agent_overrides`): поле
`level` в теле вебхука — часть сообщения модели, а не настройка."""

from types import SimpleNamespace

from fastapi.testclient import TestClient
from gc_core.settings import settings_of
from gc_server.webhook import surface as webhook_module
from gc_testkit import as_coro
from server_harness import make_webhook


def test_level_in_the_request_body_is_not_a_level_input(monkeypatch, tmp_path):
    seen = {}
    ctx = SimpleNamespace()
    surface = make_webhook(
        tmp_path,
        **{
            "level": "default",
            "agent.auto_routing": True,
            "backend.api_mode": "chat_completions",
            "models": {"fast": {}, "mid": {}, "big": {}},
            "slots": {"light": "fast", "default": "mid", "max": "big"},
        },
    )
    monkeypatch.setattr(
        webhook_module,
        "acreate_run_context",
        as_coro(lambda cfg, **kw: seen.setdefault("cfg", cfg) and ctx),
    )
    monkeypatch.setattr(webhook_module, "build_auth_connector", lambda c: None)
    monkeypatch.setattr(surface.registry, "astart", as_coro(lambda **kw: True))
    resp = TestClient(surface.app()).post(
        "/events/webhook", json={"task_id": "t2", "level": "auto"}
    )
    assert resp.status_code == 200
    assert settings_of(seen["cfg"]).level == "default"
