"""Повтор с системным трастом обязан идти СВЕЖИМ клиентом.

`truststore.inject_into_ssl()` действует на SSL-контексты, созданные ПОСЛЕ
инъекции. Общий клиент ядра (`NO_NETRC_SESSION`) строится при импорте модуля,
то есть заведомо раньше: повтор тем же клиентом шёл со старыми корнями certifi
и падал так же. Человек видел «STT недоступен» дважды и вдвое дольше, а
инъекция — единственное, ради чего повтор вообще затевался.

Через прокси дефект не виден: там под запрос поднимается отдельный транспорт,
и его контекст строится уже после инъекции. Поэтому проверка — на прямом
подключении, без прокси.
"""
import ssl

import httpx
import pytest
from gc_tui.voice_runtime import net


class _Recorder:
    """Двойник общего клиента: первый POST — провал TLS, второй — успех."""

    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            exc = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]")
            exc.__cause__ = ssl.SSLError("certificate verify failed")
            raise exc
        return httpx.Response(200, json={"text": "ок"})


@pytest.fixture
def clean_trust():
    net.reset_for_tests()
    yield
    net.reset_for_tests()


def test_retry_after_injection_asks_for_a_fresh_client(monkeypatch, clean_trust):
    """Первый заход — общим клиентом, повтор после инъекции — свежим."""
    recorder = _Recorder()
    injected = []
    monkeypatch.setattr(net, "_inject", lambda: injected.append(True))
    monkeypatch.setattr(
        "gc_core.utils.http_session.NO_NETRC_SESSION", recorder, raising=True
    )

    response = net.with_system_trust_retry(
        lambda: net.post("https://stt.example/v1/audio/transcriptions", files={})
    )

    assert response.status_code == 200
    assert injected == [True], "системный траст не инжектировали"
    assert len(recorder.calls) == 2, "повтора не было"
    assert not recorder.calls[0].get("fresh_client"), (
        "первый заход и так идёт общим клиентом — свежий там ни к чему"
    )
    assert recorder.calls[1].get("fresh_client") is True, (
        "повтор пошёл тем же клиентом: его SSL-контекст создан до инъекции, "
        "и системный траст на него не подействует"
    )


def test_loopback_request_stays_on_the_shared_client(monkeypatch, clean_trust):
    """Локальная батарейка ходит по http на loopback — свежий клиент не нужен."""
    recorder = _Recorder()
    monkeypatch.setattr(
        "gc_core.utils.http_session.NO_NETRC_SESSION", recorder, raising=True
    )

    with pytest.raises(httpx.ConnectError):
        net.post("http://127.0.0.1:8735/v1/audio/transcriptions", files={})

    assert recorder.calls[0].get("proxies") is None, "к loopback пошли через прокси"
    assert not recorder.calls[0].get("fresh_client")
