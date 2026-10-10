"""backend.proxy → settings.backend.proxy → proxies при создании httpx-клиента.

С Задачи 9 прокси едут в httpx-клиент (mounts по схемам), а не kwargs
запроса, — поэтому наблюдаем параметры создания клиента.
"""

from __future__ import annotations

import os
import tempfile

import yaml
from gc_core.config import load_settings
from gc_core.config_modules.backend import BackendConfig
from gc_core.utils import connect_backend
from gc_testkit import run_coro
from transport_fakes import install_transport


def test_backend_config_proxy_default_none():
    assert BackendConfig().proxy is None


def test_backend_config_proxy_strips_and_empty_to_none():
    assert (
        BackendConfig(proxy="  socks5h://p:1080  ").proxy == "socks5h://p:1080"
    )
    assert BackendConfig(proxy="").proxy is None
    assert BackendConfig(proxy="   ").proxy is None


def test_load_config_maps_backend_proxy():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg_path = os.path.join(tmp_dir, "config.yml")
        with open(cfg_path, "w", encoding="utf-8") as fd:
            yaml.safe_dump(
                {
                    "backend": {
                        "api_mode": "chat_completions",
                        "proxy": "socks5h://proxy.example:1080",
                    }
                },
                fd,
            )
        cfg = load_settings(cfg_path).settings
        assert cfg.backend.proxy == "socks5h://proxy.example:1080"


def test_load_config_proxy_null_maps_none():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg_path = os.path.join(tmp_dir, "config.yml")
        with open(cfg_path, "w", encoding="utf-8") as fd:
            yaml.safe_dump(
                {"backend": {"api_mode": "chat_completions", "proxy": None}},
                fd,
            )
        cfg = load_settings(cfg_path).settings
        assert cfg.backend.proxy is None


def test_chat_completions_passes_resolved_proxies(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    spy = install_transport(monkeypatch)
    run_coro(
        connect_backend.arequest_backend_chat_completions(
            {"messages": []},
            {},
            url="https://api.example/v1/chat",
            use_retry=False,
            config_proxy="socks5h://proxy.example:1080",
        )
    )
    assert spy.last_client_kwargs["proxies"] == {
        "http": "socks5h://proxy.example:1080",
        "https": "socks5h://proxy.example:1080",
    }


def test_chat_completions_falls_back_to_env_proxy(monkeypatch):
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy:8080")

    spy = install_transport(monkeypatch)
    run_coro(
        connect_backend.arequest_backend_chat_completions(
            {"messages": []},
            {},
            url="https://api.example/v1/chat",
            use_retry=False,
            config_proxy=None,
        )
    )
    assert spy.last_client_kwargs["proxies"] == {
        "http": "http://env-proxy:8080",
        "https": "http://env-proxy:8080",
    }


def test_chat_completions_no_proxy_bypasses_config(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "api.example")

    spy = install_transport(monkeypatch)
    run_coro(
        connect_backend.arequest_backend_chat_completions(
            {"messages": []},
            {},
            url="https://api.example/v1/chat",
            use_retry=False,
            config_proxy="http://proxy.example:8080",
        )
    )
    assert spy.last_client_kwargs["proxies"] is None


def test_trust_env_stays_false():
    """Контракт один на оба клиента: ~/.netrc и env-прокси не читаем.

    Синхронный общий клиент (`NO_NETRC_SESSION`) из `connect_backend` ушёл
    вместе с легаси-транспортом, но живёт для `model_picker` и `prod_auth` —
    его и проверяем по месту, а не через реэкспорт."""
    from gc_core.utils.async_http import get_async_client
    from gc_core.utils.http_session import NO_NETRC_SESSION

    assert NO_NETRC_SESSION.trust_env is False

    async def _check():
        from gc_core.utils.async_http import aclose_clients

        try:
            assert get_async_client().trust_env is False
        finally:
            await aclose_clients()

    import asyncio

    asyncio.run(_check())
