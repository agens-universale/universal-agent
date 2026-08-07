"""Пресеты адресов провайдеров для полей «Адрес API-сервера» (спека умных форм §3.1).
Справочник данных — без TUI-логики (слой config)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class EndpointPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    url: str


DEFAULT_ENDPOINT_PRESETS: tuple[EndpointPreset, ...] = (
    EndpointPreset(name="OpenRouter", url="https://openrouter.ai/api/v1/chat/completions"),
    EndpointPreset(name="Cloud.ru", url="https://foundation-models.api.cloud.ru/v1/chat/completions"),
    EndpointPreset(name="GigaCode", url="https://litellm-proxy.sberai-rnd.ru/v1/chat/completions"),
)


def default_endpoint_presets() -> list[EndpointPreset]:
    return list(DEFAULT_ENDPOINT_PRESETS)


def endpoint_presets_from_config(cfg: dict) -> list[EndpointPreset]:
    """Пресеты из сырого config-dict; ключа нет или содержимое не по форме —
    кодовый дефолт (TUI работает и на невалидированных dict'ах тестов/легаси)."""
    raw = (cfg or {}).get("endpoint_presets")
    if not isinstance(raw, list) or not raw:
        return default_endpoint_presets()
    out: list[EndpointPreset] = []
    for item in raw:
        try:
            out.append(EndpointPreset.model_validate(item))
        except Exception:
            return default_endpoint_presets()
    return out
