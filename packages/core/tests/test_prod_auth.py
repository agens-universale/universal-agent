from unittest.mock import MagicMock, patch

from gc_core.config_modules.auth import build_auth_connector
from gc_core.utils.prod_auth import AuthConnector
from gc_testkit.settings import make_settings


def _make_connector(**kwargs):
    defaults = dict(
        entry_point="http://ep",
        env_name="e",
        env_version="1",
        plugin_name="p",
        plugin_version="1",
        device_code="dc",
        device_code_hex="hex",
        login="l",
        verify=False,
    )
    defaults.update(kwargs)
    return AuthConnector(**defaults)


def test_get_token_caches_within_ttl(monkeypatch):
    c = _make_connector()
    tokens = iter(["tok1", "tok2"])
    monkeypatch.setattr(
        c, "get_auth_url", lambda url, verify, config_proxy=None: "http://auth"
    )
    monkeypatch.setattr(
        c,
        "create_token",
        lambda *a, **k: {"access_token": next(tokens), "expires_in": 600},
    )

    assert c.get_token() == "tok1"
    assert (
        c.get_token() == "tok1"
    )  # из кэша (TTL не истёк), повторного выпуска нет


def test_get_token_force_bypasses_cache(monkeypatch):
    c = _make_connector()
    tokens = iter(["tok1", "tok2"])
    monkeypatch.setattr(
        c, "get_auth_url", lambda url, verify, config_proxy=None: "http://auth"
    )
    monkeypatch.setattr(
        c,
        "create_token",
        lambda *a, **k: {"access_token": next(tokens), "expires_in": 600},
    )

    assert c.get_token() == "tok1"
    assert (
        c.get_token(force=True) == "tok2"
    )  # форс-рефреш в обход валидного TTL


def test_get_token_passes_config_proxy(monkeypatch):
    c = _make_connector(config_proxy="socks5h://127.0.0.1:1080")
    seen = {}

    def fake_get_auth_url(url, verify, config_proxy=None):
        seen["get"] = config_proxy
        return "http://auth"

    def fake_create_token(*a, **k):
        seen["create"] = k.get("config_proxy")
        return {"access_token": "tok", "expires_in": 600}

    monkeypatch.setattr(c, "get_auth_url", fake_get_auth_url)
    monkeypatch.setattr(c, "create_token", fake_create_token)

    assert c.get_token() == "tok"
    assert seen["get"] == "socks5h://127.0.0.1:1080"
    assert seen["create"] == "socks5h://127.0.0.1:1080"


def test_get_auth_url_uses_resolved_proxies(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    fake = MagicMock()
    fake.status_code = 401
    fake.headers = {"X-Authorization-Url": "https://auth.example/token"}
    with patch(
        "gc_core.utils.prod_auth._NO_NETRC_SESSION.get", return_value=fake
    ) as get:
        url = AuthConnector.get_auth_url(
            "https://gigacode.example/api/v2/config",
            verify=True,
            config_proxy="socks5h://127.0.0.1:1080",
        )
    assert url == "https://auth.example/token"
    assert get.call_args.kwargs["proxies"] == {
        "http": "socks5h://127.0.0.1:1080",
        "https": "socks5h://127.0.0.1:1080",
    }


def test_create_token_uses_resolved_proxies(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    fake = MagicMock()
    fake.json.return_value = {"access_token": "t", "expires_in": 60}
    with patch(
        "gc_core.utils.prod_auth._NO_NETRC_SESSION.post", return_value=fake
    ) as post:
        AuthConnector.create_token(
            "https://auth.example/token",
            "e",
            "1",
            "p",
            "1",
            "hex",
            "l",
            True,
            config_proxy="http://proxy:8080",
        )
    assert post.call_args.kwargs["proxies"] == {
        "http": "http://proxy:8080",
        "https": "http://proxy:8080",
    }


def test_get_auth_url_direct_when_no_proxy(monkeypatch):
    """Без config/env — proxies=None (gigacode direct, как раньше без HTTPS_PROXY)."""
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    fake = MagicMock()
    fake.status_code = 401
    fake.headers = {"X-Authorization-Url": "https://auth.example/token"}
    with patch(
        "gc_core.utils.prod_auth._NO_NETRC_SESSION.get", return_value=fake
    ) as get:
        AuthConnector.get_auth_url("https://gigacode.example/api", verify=True)
    assert get.call_args.kwargs["proxies"] is None


def test_build_auth_connector_passes_proxy():
    c = build_auth_connector(
        make_settings(
            {
                "backend.use_ssl": True,
                "backend.proxy": "socks5h://127.0.0.1:1080",
                "agent_auth": {"auth_endpoint": "https://auth.example/config"},
            }
        )
    )
    assert c is not None
    assert c.config_proxy == "socks5h://127.0.0.1:1080"


def test_build_auth_connector_none_without_agent_auth():
    assert (
        build_auth_connector(make_settings({"backend.proxy": "socks5h://x:1"}))
        is None
    )
