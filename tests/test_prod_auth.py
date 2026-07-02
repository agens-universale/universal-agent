from utils.prod_auth import AuthConnector


def _make_connector():
    return AuthConnector(
        entry_point="http://ep", env_name="e", env_version="1",
        plugin_name="p", plugin_version="1", device_code="dc",
        device_code_hex="hex", login="l", verify=False,
    )


def test_get_token_caches_within_ttl(monkeypatch):
    c = _make_connector()
    tokens = iter(["tok1", "tok2"])
    monkeypatch.setattr(c, "get_auth_url", lambda url, verify: "http://auth")
    monkeypatch.setattr(c, "create_token", lambda *a, **k: {"access_token": next(tokens), "expires_in": 600})

    assert c.get_token() == "tok1"
    assert c.get_token() == "tok1"  # из кэша (TTL не истёк), повторного выпуска нет


def test_get_token_force_bypasses_cache(monkeypatch):
    c = _make_connector()
    tokens = iter(["tok1", "tok2"])
    monkeypatch.setattr(c, "get_auth_url", lambda url, verify: "http://auth")
    monkeypatch.setattr(c, "create_token", lambda *a, **k: {"access_token": next(tokens), "expires_in": 600})

    assert c.get_token() == "tok1"
    assert c.get_token(force=True) == "tok2"  # форс-рефреш в обход валидного TTL
