"""Пресеты адресов STT-сервисов для поля «URL» голосового ввода (спека
2026-08-05-voice-endpoint-presets §2). Зеркало endpoint_presets: та же модель
записи, тот же контракт чтения — справочник данных без TUI-логики."""

from __future__ import annotations

from typing import Any

from gc_core.config_modules.endpoint_presets import EndpointPreset

DEFAULT_VOICE_ENDPOINT_PRESETS: tuple[EndpointPreset, ...] = (
    EndpointPreset(
        name="OpenAI", url="https://api.openai.com/v1/audio/transcriptions"
    ),
    EndpointPreset(
        name="Groq", url="https://api.groq.com/openai/v1/audio/transcriptions"
    ),
    EndpointPreset(
        name="Mistral", url="https://api.mistral.ai/v1/audio/transcriptions"
    ),
)


def default_voice_endpoint_presets() -> list[EndpointPreset]:
    return list(DEFAULT_VOICE_ENDPOINT_PRESETS)


def voice_endpoint_presets_from_config(cfg: Any) -> list[EndpointPreset]:
    """Пресеты ``voice_endpoint_presets`` настроек (всё, что принимает
    ``settings_of``); пустой список — кодовый дефолт (контракт
    endpoint_presets_from_config, дословно)."""
    # Ленивый импорт: settings → root → этот модуль.
    from gc_core.settings import settings_of

    return list(settings_of(cfg).voice_endpoint_presets) or (
        default_voice_endpoint_presets()
    )
