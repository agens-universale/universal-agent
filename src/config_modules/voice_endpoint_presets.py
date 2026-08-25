"""Пресеты адресов STT-сервисов для поля «URL» голосового ввода (спека
2026-08-05-voice-endpoint-presets §2). Зеркало endpoint_presets: та же модель
записи, тот же контракт чтения — справочник данных без TUI-логики."""
from __future__ import annotations

from config_modules.endpoint_presets import EndpointPreset

DEFAULT_VOICE_ENDPOINT_PRESETS: tuple[EndpointPreset, ...] = (
    EndpointPreset(name="OpenAI", url="https://api.openai.com/v1/audio/transcriptions"),
    EndpointPreset(name="Groq", url="https://api.groq.com/openai/v1/audio/transcriptions"),
    EndpointPreset(name="Mistral", url="https://api.mistral.ai/v1/audio/transcriptions"),
)


def default_voice_endpoint_presets() -> list[EndpointPreset]:
    return list(DEFAULT_VOICE_ENDPOINT_PRESETS)


def voice_endpoint_presets_from_config(cfg: dict) -> list[EndpointPreset]:
    """Пресеты из сырого config-dict; ключа нет или содержимое не по форме —
    кодовый дефолт (контракт endpoint_presets_from_config, дословно)."""
    raw = (cfg or {}).get("voice_endpoint_presets")
    if not isinstance(raw, list) or not raw:
        return default_voice_endpoint_presets()
    out: list[EndpointPreset] = []
    for item in raw:
        try:
            out.append(EndpointPreset.model_validate(item))
        except Exception:
            return default_voice_endpoint_presets()
    return out
