"""transcribe(): multipart WAV на /v1/audio/transcriptions — реальная payload-форма."""
import socket

import httpx
import pytest
from gc_testkit.net_support import tls_failure
from gc_tui.voice import transcriber as tr
from gc_tui.voice.errors import VoiceError
from gc_tui.voice_runtime import net as net


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


OPENAI_CFG = {"provider": "openai", "url": "https://stt.local/v1/audio/transcriptions",
              "token": "sk-1", "model": "whisper-1", "language": "ru",
              "timeout_seconds": 60, "verify_ssl": True}

def test_openai_happy_path(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(200, {"text": " привет мир "})

    monkeypatch.setattr(tr, "voice_post", fake_post)
    assert tr.transcribe(b"\x00\x01" * 100, OPENAI_CFG) == "привет мир"

    url, kwargs = calls[0]
    assert url == OPENAI_CFG["url"]
    assert kwargs["headers"]["Authorization"] == "Bearer sk-1"
    name, blob, mime = kwargs["files"]["file"]
    assert name == "audio.wav" and mime == "audio/wav"
    assert blob.startswith(b"RIFF")          # валидный WAV-контейнер
    assert kwargs["data"] == {"model": "whisper-1", "language": "ru"}
    assert kwargs["timeout"] == 60 and kwargs["verify"] is True


def test_openai_http_error(monkeypatch):
    monkeypatch.setattr(tr, "voice_post", lambda url, **kw: FakeResponse(500))
    with pytest.raises(VoiceError, match="HTTP 500"):
        tr.transcribe(b"\x00", OPENAI_CFG)


def test_openai_empty_text_is_error(monkeypatch):
    monkeypatch.setattr(tr, "voice_post", lambda url, **kw: FakeResponse(200, {"text": "  "}))
    with pytest.raises(VoiceError, match="ничего не распознано"):
        tr.transcribe(b"\x00", OPENAI_CFG)


class HtmlResponse(FakeResponse):
    """Портал гостевого Wi-Fi / корп-прокси: код 200, а в теле HTML-страница."""

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def test_proxy_page_becomes_human_error(monkeypatch):
    """200 ещё не значит «ответил STT». Сырой JSONDecodeError раньше пролетал мимо
    VoiceError прямо в тост — человек видел «Expecting value: line 1 column 1»."""
    monkeypatch.setattr(tr, "voice_post", lambda url, **kw: HtmlResponse(200))
    with pytest.raises(VoiceError, match="не по протоколу"):
        tr.transcribe(b"\x00", OPENAI_CFG)


def test_non_object_payload_becomes_human_error(monkeypatch):
    """JSON-список вместо объекта: `.get` на нём падал AttributeError'ом."""
    monkeypatch.setattr(tr, "voice_post", lambda url, **kw: FakeResponse(200, ["oops"]))
    with pytest.raises(VoiceError, match="не по протоколу"):
        tr.transcribe(b"\x00", OPENAI_CFG)


def test_network_failure_becomes_human_error(monkeypatch):
    """Сеть отказала — человеку «STT недоступен», а не сырой httpx.

    Ловились `requests.RequestException`, а POST давно идёт клиентом ядра на
    httpx: не совпадало ни одно, и наружу летел `httpx.ConnectError`."""
    def boom(url, **kw):
        raise httpx.ConnectError("[Errno 61] Connection refused")

    monkeypatch.setattr(tr, "voice_post", boom)
    with pytest.raises(VoiceError, match="STT недоступен"):
        tr.transcribe(b"\x00", OPENAI_CFG)


def test_real_refused_connection_becomes_human_error():
    """То же без подмен: закрытый порт на этой же машине, настоящий httpx.

    Порт занимаем и тут же отпускаем — так отказ приходит сразу и наверняка.
    Фиксированный номер (вроде 9) в нагруженной среде даёт таймаут вместо
    отказа, и тест начинает мигать: так он и упал в полном прогоне."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    cfg = dict(OPENAI_CFG, url=f"http://127.0.0.1:{port}/v1/audio/transcriptions",
               timeout_seconds=5)
    with pytest.raises(VoiceError, match="STT недоступен"):
        tr.transcribe(b"\x00" * 320, cfg)


def test_tls_error_is_not_swallowed(monkeypatch):
    """TLS — не отказ сервиса: его обязан увидеть corp-retry, а не VoiceError.

    Иначе однократная инъекция системного траста не случится, и за корп-прокси с
    собственным CA голос будет молча «недоступен»."""
    net.reset_for_tests()
    monkeypatch.setattr(net, "_inject", lambda: None)
    calls = {"n": 0}

    def boom(url, **kw):
        calls["n"] += 1
        raise tls_failure()

    monkeypatch.setattr(tr, "voice_post", boom)
    with pytest.raises(httpx.ConnectError):
        tr.transcribe(b"\x00", OPENAI_CFG)
    assert calls["n"] == 2                     # инъекция траста и повтор
