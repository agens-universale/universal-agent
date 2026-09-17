"""Поведение httpx-клиента MCP при SOCKS в HTTP_PROXY."""

from __future__ import annotations

import asyncio

import pytest

from gc_core.tools.connect_mcp.stream_client import _is_loopback_url, _make_stream_http_client, _trust_env_for_url


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://127.0.0.1:8765/mcp", True),
        ("http://localhost:8765/mcp", True),
        ("http://[::1]:8765/mcp", True),
        ("https://mcp.example.com/mcp", False),
    ],
)
def test_is_loopback_url(url: str, expected: bool) -> None:
    assert _is_loopback_url(url) is expected
    assert _trust_env_for_url(url) is (not expected)


def test_loopback_client_ignores_socks_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Локальный MCP не должен требовать socksio при socks5h:// в HTTP_PROXY."""
    monkeypatch.setenv("http_proxy", "socks5h://127.0.0.1:1080")
    monkeypatch.setenv("HTTP_PROXY", "socks5h://127.0.0.1:1080")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    client = _make_stream_http_client(
        token=None,
        extra_headers=None,
        timeout=1.0,
        verify=True,
        base_url="http://127.0.0.1:8765/mcp",
    )
    try:
        # Раньше падало ImportError: socksio — клиент создаётся без ошибки.
        assert client is not None
    finally:
        asyncio.run(client.aclose())


def test_lookalike_host_is_not_loopback():
    """`127.0.0.1.evil.com` — обычное DNS-имя, а не петля.

    Здесь жила своя копия проверки с `hostname.startswith("127.")`, под которую
    такое имя подходило. Следствие не косметическое: адрес признавался локальным,
    `_trust_env_for_url` снимал `trust_env`, и запрос с `Authorization: Bearer`
    уходил напрямую в интернет мимо корпоративного прокси — то есть токен
    покидал периметр.

    Канон `gc_core.utils.url_addr.is_loopback` разбирает адрес через
    `ipaddress`, а не по префиксу строки, и был написан ровно против этой дыры;
    сюда его забыли переключить.

    Подмена, которую ловит тест: вернуть `hostname.startswith("127.")`.
    """
    from gc_core.tools.connect_mcp.stream_client import (
        _is_loopback_url,
        _trust_env_for_url,
    )

    for evil in (
        "http://127.0.0.1.evil.com/mcp",
        "http://127.0.0.1.attacker.example/mcp",
        "https://localhost.evil.com/mcp",
    ):
        assert not _is_loopback_url(evil), evil
        assert _trust_env_for_url(evil), f"{evil}: прокси снят, токен уйдёт мимо него"

    # Положительный контроль: настоящая петля по-прежнему обходит прокси, иначе
    # тест прошёл бы и на реализации «всё считать внешним».
    for local in ("http://127.0.0.1:8765/mcp", "http://localhost:8765/mcp", "http://[::1]:8765/mcp"):
        assert _is_loopback_url(local), local
        assert not _trust_env_for_url(local), local
