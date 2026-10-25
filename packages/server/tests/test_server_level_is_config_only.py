"""Уровень модели на сервере — только из конфига (`agent_overrides`): поле
`level` в теле вебхука — часть сообщения модели, а не настройка.

Изоляция импорта — тем же приёмом, что test_server_broken_config_wiring.py
(сброс sys.modules['server'] + патч rest_api.server_config.* ДО импорта).
"""

import sys
from unittest.mock import patch

import pytest

pytest.importorskip(
    "fastapi", reason="server-side dep, install via server_requirements.txt"
)
pytest.importorskip(
    "starlette", reason="server-side dep, install via server_requirements.txt"
)

from fastapi.testclient import TestClient
from gc_testkit import as_coro
from gc_testkit.settings import make_settings
from gc_core.settings import settings_of


def _reset_server_module():
    for mod in list(sys.modules):
        if mod == "gc_server.server" or mod.startswith("gc_server.server."):
            sys.modules.pop(mod, None)
    parent = sys.modules.get("gc_server")
    if parent is not None and hasattr(parent, "server"):
        delattr(parent, "server")


def _minimal_server_config():
    return {
        "server": {},
        "defaults": {"system": "generic"},
        "agent": {},
        "agent_overrides": {"model": "auto"},
        "logging": {},
        "swarm": {},
        "threads_cleanup": {},
    }


def _post_webhook(monkeypatch, config_dict, body):
    """Вебхук на подменённом конфиге; возвращает конфиг, с которым собран RunContext."""
    seen = {}
    with (
        patch(
            "gc_server.rest_api.server_config.load_server_config",
            return_value=_minimal_server_config(),
        ),
        patch(
            "gc_server.rest_api.server_config.build_agent_settings_for_server",
            return_value=make_settings(config_dict),
        ),
    ):
        from gc_server import server

    monkeypatch.setattr(
        server,
        "acreate_run_context",
        as_coro(lambda cfg, **kw: seen.setdefault("cfg", cfg)),
    )
    monkeypatch.setattr(server, "build_auth_connector", lambda cfg: None)
    monkeypatch.setattr(
        server.agent_run_registry,
        "astart_if_absent",
        as_coro(lambda **kw: True),
    )
    monkeypatch.setattr(
        server.agent_run_registry, "add_user_message", lambda **kw: False
    )

    client = TestClient(server.app)
    resp = client.post("/events/webhook", json=body)
    assert resp.status_code == 200
    return seen["cfg"]


def test_level_in_the_request_body_is_not_a_level_input(monkeypatch):
    """Поле `level` в теле вебхука — часть сообщения модели, а не настройка:
    уровень берётся только из конфига сервера (`agent_overrides`)."""
    _reset_server_module()
    config_dict = {
        "model": "default",
        "agent.auto_routing": True,
        "backend.api_mode": "chat_completions",
        "models": {"fast": {}, "mid": {}, "big": {}},
        "slots": {"light": "fast", "default": "mid", "max": "big"},
    }
    try:
        cfg = _post_webhook(
            monkeypatch, config_dict, {"task_id": "t2", "model": "auto"}
        )
        assert settings_of(cfg).model == "default"
    finally:
        _reset_server_module()
