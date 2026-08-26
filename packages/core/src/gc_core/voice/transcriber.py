"""STT-транскрибация: PCM16 → текст через внешний HTTP-сервис (batch).

Один протокол: /v1/audio/transcriptions-совместимый endpoint (multipart WAV) —
им говорят и локальная батарейка, и облачные сервисы. Все сбои — VoiceError с
человекочитаемым русским текстом (уходит в toast).
"""
from __future__ import annotations

import io
import wave
from typing import Any, Dict

import requests

from gc_core.voice.errors import VoiceError
from gc_core.voice.recorder import SAMPLE_RATE
from gc_core.voice_runtime.net import post as voice_post, with_system_trust_retry


def transcribe(pcm: bytes, cfg: Dict[str, Any]) -> str:
    """PCM16 16kHz mono → распознанный текст. Ошибки → VoiceError.

    Корп-сеть: TLS-перехват прокси даёт SSLError на первом запросе — retry
    с системным трастом (voice_runtime.net), для юзера прозрачно."""
    return with_system_trust_retry(lambda: _transcribe_openai(pcm, cfg))


def _pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buf.getvalue()


def _transcribe_openai(pcm: bytes, cfg: Dict[str, Any]) -> str:
    try:
        resp = voice_post(
            cfg.get("url", ""),
            headers={"Authorization": f"Bearer {cfg.get('token', '')}"},
            files={"file": ("audio.wav", _pcm_to_wav(pcm), "audio/wav")},
            data={"model": cfg.get("model", "whisper-1"),
                  "language": cfg.get("language", "ru")},
            timeout=cfg.get("timeout_seconds", 60),
            verify=cfg.get("verify_ssl", True),
            config_proxy=cfg.get("proxy"),
        )
    except requests.exceptions.SSLError:
        raise                                  # corp-retry в transcribe()
    except requests.RequestException as exc:
        raise VoiceError(f"STT недоступен: {exc}") from exc
    if resp.status_code != 200:
        raise VoiceError(f"STT: HTTP {resp.status_code}")
    # Тело читаем ПОД обработкой: 200 ещё не значит «ответил сервис». Корп-прокси и
    # порталы гостевого Wi-Fi отдают свою HTML-страницу с кодом 200, и сырой
    # JSONDecodeError («Expecting value: line 1 column 1») пролетал мимо VoiceError
    # прямо человеку в тост — вместо внятного текста, по которому видно, что делать.
    try:
        payload = resp.json()
    except ValueError:
        raise VoiceError("STT ответил не по протоколу (не JSON) — "
                         "похоже, в ответ пришла страница прокси или портала") from None
    if not isinstance(payload, dict):
        raise VoiceError("STT ответил не по протоколу (в ответе не объект)")
    text = (payload.get("text") or "").strip()
    if not text:
        raise VoiceError("ничего не распознано")
    return text
