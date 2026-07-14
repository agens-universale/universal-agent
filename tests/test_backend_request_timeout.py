"""backend.request_timeout_sec → config_dict[backend_request_timeout_sec] → HTTP timeout."""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
import yaml

from config import load_config
from config_modules.backend import DEFAULT_REQUEST_TIMEOUT_SEC, BackendConfig
from utils import connect_backend


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
        cfg = load_config(cfg_path)
        assert cfg["backend_request_timeout_sec"] == 120


def test_load_config_timeout_default_when_omitted():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg_path = os.path.join(tmp_dir, "config.yml")
        with open(cfg_path, "w", encoding="utf-8") as fd:
            fd.write("backend:\n  api_mode: chat_completions\n")
        cfg = load_config(cfg_path)
        assert cfg["backend_request_timeout_sec"] == DEFAULT_REQUEST_TIMEOUT_SEC


def test_chat_completions_passes_timeout():
    fake = MagicMock()
    fake.json.return_value = {"id": "x", "choices": [{"message": {"content": "ok"}}]}
    fake.raise_for_status.return_value = None
    with patch.object(connect_backend._NO_NETRC_SESSION, "post", return_value=fake) as post:
        connect_backend.request_backend_chat_completions(
            {"messages": []},
            {},
            url="https://b/v1/chat",
            use_retry=False,
            timeout=120,
        )
    assert post.call_args.kwargs["timeout"] == 120


def test_chat_completions_default_timeout():
    fake = MagicMock()
    fake.json.return_value = {"id": "x", "choices": [{"message": {"content": "ok"}}]}
    fake.raise_for_status.return_value = None
    with patch.object(connect_backend._NO_NETRC_SESSION, "post", return_value=fake) as post:
        connect_backend.request_backend_chat_completions(
            {"messages": []}, {}, url="https://b/v1/chat", use_retry=False
        )
    assert post.call_args.kwargs["timeout"] == DEFAULT_REQUEST_TIMEOUT_SEC


def test_policy_forwards_timeout(monkeypatch):
    import request_backend_policy as policy

    seen = {}

    def fake_req(*_args, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(policy, "request_backend_chat_completions", fake_req)
    policy.request_chat_completions_with_policy(
        payload_factory=lambda mt: {"max_tokens": mt},
        initial_max_tokens=100,
        min_response_tokens=10,
        headers={},
        url="http://x",
        verify=True,
        use_retry=False,
        timeout=90,
    )
    assert seen["timeout"] == 90
