"""Явный Authorization не должен перетираться кредами из ~/.netrc.

История: на requests это была реальная ловушка. ``requests.Session`` при
``trust_env=True`` читает ``~/.netrc``, и Basic-auth оттуда ЗАМЕЩАЕТ явно
переданный ``Authorization: Bearer …`` — запрос к модели уходил с чужими
кредами и возвращал 401. Ради этого и появилась ``NO_NETRC_SESSION`` с
``trust_env=False``.

После переезда ядра на httpx ловушки нет структурно: httpx НЕ читает netrc
сам, ``NetRCAuth`` нужно передать явно как ``auth=``. Но проверка остаётся —
она сторожит свойство, а не реализацию: если кто-то вернёт requests или
подключит NetRCAuth, тест покраснеет.

``trust_env=False`` при этом всё ещё значим: у httpx он отключает env-прокси и
CA из окружения, а их мы передаём явно (см. ``resolve_proxies``).
"""

import httpx
from gc_core.utils.http_session import NO_NETRC_SESSION


def _netrc_at(tmp_path, monkeypatch) -> None:
    """Подсунуть ~/.netrc с кредами на любой хост."""
    netrc = tmp_path / ".netrc"
    netrc.write_text(
        "machine example.com login netrc_user password netrc_pass\n",
        encoding="utf-8",
    )
    netrc.chmod(0o600)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NETRC", str(netrc))


def test_explicit_authorization_survives_netrc(tmp_path, monkeypatch):
    """Наш клиент отправляет ровно тот Authorization, который ему дали."""
    _netrc_at(tmp_path, monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={})

    # Транспорт подменяем, всё остальное — настоящий клиент ядра.
    original = NO_NETRC_SESSION._transport
    NO_NETRC_SESSION._transport = httpx.MockTransport(handler)
    try:
        NO_NETRC_SESSION.post("http://example.com/v1", headers={"Authorization": "Bearer REAL"})
    finally:
        NO_NETRC_SESSION._transport = original

    assert seen["auth"] == "Bearer REAL"


def test_client_does_not_trust_env():
    """trust_env=False: env-прокси и CA из окружения не подхватываются.

    Их резолвит resolve_proxies и передаёт явно — иначе поведение зависело бы
    от того, что стоит в шелле у конкретного пользователя.
    """
    assert NO_NETRC_SESSION.trust_env is False


def test_httpx_does_not_read_netrc_implicitly(tmp_path, monkeypatch):
    """Фиксируем сам факт: у httpx netrc-аутентификация только явная.

    Если это когда-нибудь изменится (или кто-то передаст NetRCAuth), тест
    покраснеет и напомнит, зачем существует NO_NETRC_SESSION.
    """
    _netrc_at(tmp_path, monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler), trust_env=True) as c:
        c.get("http://example.com/v1")

    assert seen["auth"] is None, "httpx начал читать netrc сам — проверить ядро"
