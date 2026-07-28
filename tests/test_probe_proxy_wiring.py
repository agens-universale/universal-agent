"""Probe/settings transport: config_proxy → resolve_proxies → requests Session."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import model_picker as mp


def test_requests_get_ex_passes_resolved_proxies(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"data": [{"id": "m1"}]}
    with patch("utils.http_session.NO_NETRC_SESSION.get", return_value=fake) as get:
        reply = mp._requests_get_ex(
            "https://openrouter.ai/api/v1/models",
            {},
            verify=True,
            config_proxy="socks5h://127.0.0.1:1080",
        )
    assert reply.status == 200
    assert reply.payload == {"data": [{"id": "m1"}]}
    assert get.call_args.kwargs["proxies"] == {
        "http": "socks5h://127.0.0.1:1080",
        "https": "socks5h://127.0.0.1:1080",
    }


def test_probe_fetch_default_uses_config_proxy(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    seen = {}

    def fake_get_ex(url, headers, verify, *, config_proxy=None):
        seen["config_proxy"] = config_proxy
        seen["url"] = url
        return mp.HttpReply(status=200, payload={"data": [{"id": "m1"}]})

    monkeypatch.setattr(mp, "_requests_get_ex", fake_get_ex)
    out = mp.probe_fetch(
        "https://openrouter.ai/api/v1/chat/completions",
        None,
        config_proxy="socks5h://127.0.0.1:1080",
    )
    assert out.models and out.models[0].id == "m1"
    assert seen["config_proxy"] == "socks5h://127.0.0.1:1080"
    assert seen["url"].endswith("/config") or seen["url"].endswith("/models")


def test_settings_probe_forwards_config_proxy(monkeypatch):
    from tui.services import settings_probe as sp

    seen = {}

    def fake_fetch(endpoint, auth, *, verify=True, config_proxy=None, **kw):
        seen["config_proxy"] = config_proxy
        return mp.FetchOutcome(
            models=[mp.ModelInfo(id="m1")], status=200, network_error="", latency_ms=1,
        )

    monkeypatch.setattr(mp, "probe_fetch", fake_fetch)
    r = sp.probe(
        "https://x/v1/chat/completions", None, True, "m1",
        config_proxy="socks5h://127.0.0.1:1080",
    )
    assert r.verdict == "ok"
    assert seen["config_proxy"] == "socks5h://127.0.0.1:1080"
