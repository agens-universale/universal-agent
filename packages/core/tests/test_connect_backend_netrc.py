import requests


def test_no_netrc_session_keeps_explicit_auth(monkeypatch):
    from gc_core.utils.connect_backend import _NO_NETRC_SESSION
    # имитируем, что netrc отдаёт Basic-креды для любого хоста
    monkeypatch.setattr(
        requests.sessions, "get_netrc_auth",
        lambda url, raise_errors=False: ("netrc_user", "netrc_pass"),
    )
    req = requests.Request("POST", "http://example.com/v1",
                           headers={"Authorization": "Bearer REAL"})

    # наша сессия (trust_env=False) — netrc НЕ зовётся, явный заголовок цел
    ours = _NO_NETRC_SESSION.prepare_request(req)
    assert ours.headers["Authorization"] == "Bearer REAL"

    # контраст: обычная Session (trust_env=True) — netrc перетёр на Basic
    theirs = requests.Session().prepare_request(req)
    assert theirs.headers["Authorization"].startswith("Basic ")
