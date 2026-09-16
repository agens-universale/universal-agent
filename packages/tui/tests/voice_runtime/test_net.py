"""Сеть голоса: corp-TLS retry и единая точка POST (сессия без ~/.netrc + прокси)."""
import ssl

import pytest

# Форма отказа — общая на два набора, поэтому из gc_testkit. Раньше тесты кидали
# `requests.exceptions.SSLError`: форму, которой на этом пути не бывает с
# переезда ядра на httpx, — и проверяли ветку, которой нет.
from gc_testkit.net_support import tls_failure

from gc_tui.voice_runtime import net as net


@pytest.fixture(autouse=True)
def _reset():
    net.reset_for_tests()


def test_passthrough_without_error(monkeypatch):
    calls = {"inject": 0}
    monkeypatch.setattr(net, "_inject", lambda: calls.__setitem__("inject", calls["inject"] + 1))
    assert net.with_system_trust_retry(lambda: 42) == 42
    assert calls["inject"] == 0            # без ошибки траст не трогаем


def test_ssl_error_injects_once_and_retries(monkeypatch):
    calls = {"inject": 0, "fn": 0}
    monkeypatch.setattr(net, "_inject", lambda: calls.__setitem__("inject", calls["inject"] + 1))

    def fn():
        calls["fn"] += 1
        if calls["fn"] == 1:
            raise tls_failure()
        return "ok"

    assert net.with_system_trust_retry(fn) == "ok"
    assert calls == {"inject": 1, "fn": 2}


def test_second_call_does_not_reinject_and_does_not_retry(monkeypatch):
    """Инъекция одна на процесс — и повтор тоже. Повтор осмыслен ровно после
    переключения траста: оно и меняет исход. Дальше TLS-ошибка означает настоящую
    беду с сертификатом, и второй заход только отправил бы весь WAV по сети ещё раз
    и вдвое отложил «STT недоступен»."""
    calls = {"inject": 0}
    monkeypatch.setattr(net, "_inject", lambda: calls.__setitem__("inject", calls["inject"] + 1))
    flaky = {"n": 0}

    def fn():
        flaky["n"] += 1
        if flaky["n"] == 1:
            raise ssl.SSLError("boom")
        return 1

    net.with_system_trust_retry(fn)
    assert calls["inject"] == 1
    assert flaky["n"] == 2                  # ошибка → инъекция → повтор
    # Траст уже переключён: новая TLS-ошибка отдаётся человеку сразу, без второй отправки.
    flaky["n"] = 0
    with pytest.raises(ssl.SSLError):
        net.with_system_trust_retry(fn)
    assert calls["inject"] == 1
    assert flaky["n"] == 1                  # ровно один заход, WAV не ушёл дважды


def test_non_tls_error_propagates(monkeypatch):
    monkeypatch.setattr(net, "_inject", lambda: None)
    with pytest.raises(ValueError):
        net.with_system_trust_retry(lambda: (_ for _ in ()).throw(ValueError("x")))


# --- post(): чем и куда ходит голос -----------------------------------------


class _FakeSession:
    """Подменыш NO_NETRC_SESSION: запоминает, с чем позвали."""

    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return "resp"


@pytest.fixture
def session(monkeypatch):
    from gc_core.utils import http_session as hs
    fake = _FakeSession()
    monkeypatch.setattr(hs, "NO_NETRC_SESSION", fake)
    return fake


def test_post_goes_through_no_netrc_session(session, monkeypatch):
    """~/.netrc не должен перетирать наш Bearer — значит сессия только эта."""
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    assert net.post("https://api.groq.com/openai/v1/audio/transcriptions") == "resp"
    assert len(session.calls) == 1


def test_post_uses_config_proxy_for_cloud(session, monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    net.post("https://api.groq.com/openai/v1/audio/transcriptions",
             config_proxy="http://corp:3128")
    _, kwargs = session.calls[0]
    assert kwargs["proxies"] == {"http": "http://corp:3128", "https": "http://corp:3128"}


def test_post_falls_back_to_env_proxy(session, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://env:8080")
    net.post("https://api.groq.com/openai/v1/audio/transcriptions")
    _, kwargs = session.calls[0]
    assert kwargs["proxies"] == {"http": "http://env:8080", "https": "http://env:8080"}


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8123/v1/audio/transcriptions",
    "http://localhost:8123/v1/audio/transcriptions",
])
def test_post_never_proxies_local_battery(session, monkeypatch, url):
    """Локальный сервис живёт на этой же машине: корп-прокси туда не пустит."""
    monkeypatch.setenv("HTTPS_PROXY", "http://env:8080")
    net.post(url, config_proxy="http://corp:3128")
    _, kwargs = session.calls[0]
    assert kwargs["proxies"] is None
