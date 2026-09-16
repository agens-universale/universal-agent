# tests/voice/test_transcriber_corp.py
"""Корп-сеть: TLS-перехват облачного STT лечится системным трастом (retry)."""
from gc_testkit.net_support import tls_failure

from gc_tui.voice_runtime import net as net
from gc_tui.voice.transcriber import transcribe

CFG = {"provider": "openai", "url": "https://stt.corp/v1/audio/transcriptions",
       "token": "t", "model": "m"}


def test_openai_retries_after_ssl_error(monkeypatch):
    net.reset_for_tests()
    monkeypatch.setattr(net, "_inject", lambda: None)
    calls = {"n": 0}

    class Resp:
        status_code = 200
        def json(self):
            return {"text": "привет"}

    def fake_post(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise tls_failure()                # так TLS-отказ приходит из httpx
        return Resp()

    monkeypatch.setattr("gc_tui.voice.transcriber.voice_post", fake_post)
    assert transcribe(b"\x00\x00" * 160, CFG) == "привет"
    assert calls["n"] == 2
