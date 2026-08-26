"""Сборка запуска speaches, warmup-WAV и прогрев (без сети и процессов)."""
import io
import wave

from voice_runtime.service import (
    build_run_spec, make_silence_wav, preheat, service_url, transcriptions_url,
)
from voice_runtime.state import VoiceState

ST = VoiceState(version="v0.8.3", root="C:/data/voice/speaches",
                port=8735, model="Systran/faster-whisper-small")


def test_urls():
    assert service_url(ST) == "http://127.0.0.1:8735"
    assert transcriptions_url(ST) == "http://127.0.0.1:8735/v1/audio/transcriptions"


def test_build_run_spec_command_env():
    spec = build_run_spec(ST)
    assert spec.command == "uv"
    assert spec.args[:4] == ("run", "--no-sync", "--project", "C:/data/voice/speaches")
    assert "uvicorn" in spec.args and "speaches.main:create_app" in spec.args
    assert ("--port", "8735") == spec.args[-2:]
    env = dict(spec.env)
    assert env["ENABLE_UI"] == "false"      # без ui-extra create_app падает на gradio
    assert env["WHISPER__TTL"] == "-1"      # ключ пина v0.8.3 (не STT_MODEL_TTL)
    # cpu жёстко: auto у ctranslate2 = CUDA по наличию драйвера → краш без cuDNN
    assert env["WHISPER__INFERENCE_DEVICE"] == "cpu"
    # модель уже в кэше (install) — сервису HF не нужен; в корп-сети это
    # убирает TLS-зависания холодной загрузки
    assert env["HF_HUB_OFFLINE"] == "1"
    assert "WHISPER__COMPUTE_TYPE" not in env      # не хардкодим (замеры юзера)
    assert spec.detach is True


def test_make_silence_wav_valid():
    blob = make_silence_wav(0.2)
    with wave.open(io.BytesIO(blob)) as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getnframes() == 3200


def test_preheat_posts_and_swallow_errors():
    calls = {}

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls["model"] = kwargs["data"]["model"]
        calls["timeout"] = kwargs["timeout"]
        class R:  # noqa: N801 — минимальный ответ
            status_code = 200
        return R()

    assert preheat(ST, post=fake_post) is True
    assert calls["url"] == transcriptions_url(ST)
    assert calls["model"] == ST.model
    assert calls["timeout"] == 120

    def boom(url, **kwargs):
        raise OSError("connection refused")

    assert preheat(ST, post=boom) is False   # прогрев никогда не роняет вызывающего
