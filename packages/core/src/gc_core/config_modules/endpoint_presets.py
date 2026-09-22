"""Пресеты адресов провайдеров для полей «Адрес API-сервера» (спека умных форм §3.1).
Справочник данных — без TUI-логики (слой config)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class EndpointPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    url: str


# Порядок значим. Список фильтруется по подстроке имени и адреса, а курсор при
# раскрытии встаёт на совпадение с текущим значением поля, — то есть порядок виден
# ровно в одном месте: первый экран мастера, где поле пусто и Enter подставляет
# первый пункт. Поэтому первый — предложение по умолчанию, дальше прочие внешние.
#
# Внешний контур GigaCode (gigacode.ru) в пресеты пока НЕ берём: списка моделей
# у него не получить (/config и /models отвечают 401 даже на валидный ключ —
# своя plugin-авторизация), а пресет обязан вести туда, где человек заработает
# сразу, а не в тупик.
#
# Каталог живёт здесь, а не в config.default.yml: в шаблоне блок закомментирован,
# иначе он копией впечатывается в конфиг каждому при первом запуске и следующий
# добавленный провайдер до людей не доезжает.
DEFAULT_ENDPOINT_PRESETS: tuple[EndpointPreset, ...] = (
    EndpointPreset(name="OpenRouter", url="https://openrouter.ai/api/v1/chat/completions"),
    EndpointPreset(name="Cloud.ru", url="https://foundation-models.api.cloud.ru/v1/chat/completions"),
    EndpointPreset(name="RouterAI", url="https://routerai.ru/api/v1/chat/completions"),
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
