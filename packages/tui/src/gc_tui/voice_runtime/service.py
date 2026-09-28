"""Запуск/проверка/прогрев локального speaches-сервиса."""

from __future__ import annotations

import io
import socket
import wave
from pathlib import Path

from recommended_mcp.manifest import RunSpec

from gc_tui.voice_runtime.state import VoiceState

_PREHEAT_TIMEOUT_SEC = 120  # первый запрос грузит модель с диска — это долго


def service_url(state: VoiceState) -> str:
    return f"http://127.0.0.1:{state.port}"


def transcriptions_url(state: VoiceState) -> str:
    return f"{service_url(state)}/v1/audio/transcriptions"


def build_run_spec(state: VoiceState) -> RunSpec:
    """Команда сервиса: официальный python-путь speaches (uvicorn --factory)."""
    return RunSpec(
        type="command",
        detach=True,
        command="uv",
        # --no-sync: uv run иначе молча пересинхронизирует env по lock — вернёт вычищенный onnxruntime-gpu и dev-группу
        args=(
            "run",
            "--no-sync",
            "--project",
            state.root,
            "uvicorn",
            "--factory",
            "speaches.main:create_app",
            "--host",
            "127.0.0.1",
            "--port",
            str(state.port),
        ),
        # ENABLE_UI=false: create_app иначе импортирует gradio (ui-extra не ставим).
        # WHISPER__TTL: в пине v0.8.3 ttl живёт в WhisperConfig (не STT_MODEL_TTL) —
        # подтверждено дампом конфига сервиса на живой приёмке.
        # DEVICE=cpu жёстко: auto у ctranslate2 выбирает CUDA по наличию ДРАЙВЕРА
        # (не библиотек) и нативно крашится без cuDNN — поймано живой приёмкой
        # на Windows с NVIDIA-драйвером. CUDA-режим — future (install --cuda).
        # HF_HUB_OFFLINE: модель скачивает install; сервис работает из кэша.
        # Без него холодная загрузка ходит на HF (в корп-сети — TLS-зависание).
        env=(
            ("ENABLE_UI", "false"),
            ("WHISPER__TTL", "-1"),
            ("WHISPER__INFERENCE_DEVICE", "cpu"),
            ("HF_HUB_OFFLINE", "1"),
        ),
        cwd=Path(state.root),
    )


def port_alive(state: VoiceState, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection(
            ("127.0.0.1", state.port), timeout=timeout
        ):
            return True
    except OSError:
        return False


def make_silence_wav(seconds: float = 0.2) -> bytes:
    """WAV 16кГц mono int16 из тишины — дежурный запрос прогрева."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


def preheat(state: VoiceState, *, post=None) -> bool:
    """Прогреть модель дежурной транскрибацией тишины. Ошибки глотаются.

    Идёт через общий шов голоса (`net.post`): сервис живёт на loopback, и при заданном
    корп-прокси дефолтная сессия увела бы прогрев в прокси — тот в 127.0.0.1 не пустит."""
    if post is None:
        from gc_tui.voice_runtime.net import post as net_post

        post = net_post
    try:
        resp = post(
            transcriptions_url(state),
            files={"file": ("warmup.wav", make_silence_wav(), "audio/wav")},
            data={"model": state.model},
            timeout=_PREHEAT_TIMEOUT_SEC,
        )
        return getattr(resp, "status_code", 0) == 200
    except Exception:
        return False
