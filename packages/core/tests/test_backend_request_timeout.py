"""backend.request_timeout_sec → settings.backend.request_timeout_sec → HTTP timeout."""

import os
import tempfile

import pytest
import yaml
from gc_core.config import load_settings
from gc_core.config_modules.backend import (
    DEFAULT_REQUEST_TIMEOUT_SEC,
    BackendConfig,
)
from gc_core.utils import connect_backend
from gc_testkit import run_coro


def test_backend_config_timeout_default():
    assert BackendConfig().request_timeout_sec == DEFAULT_REQUEST_TIMEOUT_SEC
    assert DEFAULT_REQUEST_TIMEOUT_SEC == 600


def test_backend_config_timeout_custom():
    assert BackendConfig(request_timeout_sec=120).request_timeout_sec == 120


def test_backend_config_timeout_must_be_positive():
    with pytest.raises(Exception):
        BackendConfig(request_timeout_sec=0)
    with pytest.raises(Exception):
        BackendConfig(request_timeout_sec=-1)


def test_load_config_maps_backend_request_timeout_sec():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg_path = os.path.join(tmp_dir, "config.yml")
        with open(cfg_path, "w", encoding="utf-8") as fd:
            yaml.safe_dump(
                {
                    "backend": {
                        "api_mode": "chat_completions",
                        "request_timeout_sec": 120,
                    }
                },
                fd,
            )
        cfg = load_settings(cfg_path).settings
        assert cfg.backend.request_timeout_sec == 120


def test_load_config_timeout_default_when_omitted():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg_path = os.path.join(tmp_dir, "config.yml")
        with open(cfg_path, "w", encoding="utf-8") as fd:
            fd.write("backend:\n  api_mode: chat_completions\n")
        cfg = load_settings(cfg_path).settings
        assert cfg.backend.request_timeout_sec == DEFAULT_REQUEST_TIMEOUT_SEC


def _install_capturing_transport(monkeypatch, seen):
    """Подменить фабрику httpx-клиента, запомнив таймаут запроса.

    С Задачи 9 таймаут передаётся на уровне ЗАПРОСА, а не клиента: клиент
    переиспользуется между вызовами, и зашитый в него таймаут молча применился
    бы к чужому запросу.
    """
    import httpx

    class _Capturing(httpx.AsyncClient):
        async def post(self, *args, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            return await super().post(*args, **kwargs)

    def handler(request):
        return httpx.Response(
            200, json={"id": "x", "choices": [{"message": {"content": "ok"}}]}
        )

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **kw: _Capturing(transport=httpx.MockTransport(handler)),
    )


def test_chat_completions_passes_timeout(monkeypatch):
    seen = {}
    _install_capturing_transport(monkeypatch, seen)
    run_coro(
        connect_backend.arequest_backend_chat_completions(
            {"messages": []},
            {},
            url="https://b/v1/chat",
            use_retry=False,
            timeout=120,
        )
    )
    assert seen["timeout"] == 120


def test_chat_completions_default_timeout(monkeypatch):
    seen = {}
    _install_capturing_transport(monkeypatch, seen)
    run_coro(
        connect_backend.arequest_backend_chat_completions(
            {"messages": []},
            {},
            url="https://b/v1/chat",
            use_retry=False,
        )
    )
    assert seen["timeout"] == DEFAULT_REQUEST_TIMEOUT_SEC


def test_policy_forwards_timeout(monkeypatch):
    from gc_core import request_backend_policy as policy

    seen = {}

    async def fake_req(*_args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(policy, "arequest_backend_chat_completions", fake_req)
    run_coro(
        policy.arequest_chat_completions_with_policy(
            payload_factory=lambda mt: {"max_tokens": mt},
            initial_max_tokens=100,
            min_response_tokens=10,
            headers={},
            url="http://x",
            verify=True,
            use_retry=False,
            timeout=90,
        )
    )
    assert seen["timeout"] == 90
