"""transcribe(): multipart WAV на /v1/audio/transcriptions — реальная payload-форма."""
import pytest

from gc_tui.voice import transcriber as tr
from gc_tui.voice.errors import VoiceError


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
