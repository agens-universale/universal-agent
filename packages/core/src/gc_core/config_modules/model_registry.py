"""Секции конфига `models`/`slots`/`assign` — реестр моделей, уровни, привязки потребителей.

Фаза 1 (эта фаза): только схема + пофайловые валидаторы (типы, зарезервированные
имена, конфликты дублирующих настроек одной модели). Ссылочная целостность
(слот → существующий алиас, привязка → уровень/алиас из merged-реестра) — фаза 2,
вне этого модуля.

Как это настраивается пользователем — docs/configuration.md, раздел
«Профили и уровни моделей».
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from gc_core.config_modules.backend import BackendConfig
from gc_core.config_modules.pricing import ModelTariff

RESERVED_WORDS = ("light", "default", "max", "auto")
LEVELS = ("light", "default", "max")

# Матрица фоллбэков уровня (спека §4): порядок обхода слотов, если слот
# запрошенного уровня не задан. Живёт рядом с LEVELS — потребитель (ModelRouter,
# транспортный слой) не должен ради трёх кортежей тянуть всё дерево AppConfig.
LEVEL_FALLBACK: dict[str, tuple[str, str, str]] = {
    "light": ("light", "default", "max"),
    "default": ("default", "max", "light"),
    "max": ("max", "default", "light"),
}


def validate_model_alias(alias: str) -> None:
    """Алиас models.<alias> не может совпадать с зарезервированным словом уровня."""
    if alias in RESERVED_WORDS:
        raise ValueError(
            f"models: имя '{alias}' зарезервировано ({', '.join(RESERVED_WORDS)})"
        )


def validate_level_value(value: str) -> str:
    """level ∉ LEVELS — ошибка; auto получает отдельную подсказку (появится в волне C)."""
    if value == "auto":
        raise ValueError(
            "level: значение 'auto' появится с функцией авто-выбора — пока недоступно"
        )
    if value not in LEVELS:
        raise ValueError(f"level должен быть одним из {LEVELS}, получено '{value}'")
    return value


def validate_registry_pairs(raw: dict, source: str = "") -> None:
    """§3, точка B: проверка правила пары на СЫРОМ документе одного слоя, до слияния.

    Нужна отдельно от валидатора `ModelEntry`: слои сливаются `deep_merge` рекурсивно по
    ключам, поэтому слой, задавший ОДИН адрес поверх чужой полной пары, даёт итоговую
    запись с полной парой — и проверка итогового документа молчит, пока токен базы уходит
    на адрес слоя. Здесь же видно, ЧЕЙ это файл.

    Работает по сырому словарю намеренно: `url: null` («слой снимает адрес») отличается от
    отсутствия ключа только наличием ключа, и после pydantic это различие уже потеряно."""
    models = (raw or {}).get("models")
    if not isinstance(models, dict):
        return
    prefix = f"{source}: " if source else ""
    for alias, entry in models.items():
        if not isinstance(entry, dict):
            continue
        backend = entry.get("backend") if isinstance(entry.get("backend"), dict) else {}
        headers = (backend.get("chat_completion_headers")
                   if isinstance(backend.get("chat_completion_headers"), dict) else {})
        has_address = bool(entry.get("url") or backend.get("completions_url")
                           or backend.get("proxy"))
        has_token = "token" in entry and entry["token"] is not None
        has_token = has_token or headers.get("Authorization") is not None
        if has_address and not has_token:
            raise ValueError(
                f'{prefix}models.{alias}: задан свой адрес, но не задан токен — укажите '
                'token (или token: "" — «сервер не требует авторизации»), либо уберите '
                "адрес, чтобы запись работала через общее подключение"
            )
        if has_token and not has_address:
            raise ValueError(
                f"{prefix}models.{alias}: задан свой токен, но не задан адрес — укажите "
                "url, либо уберите token, чтобы запись работала через общее подключение"
            )


class ModelEntry(BaseModel):
    """Запись реестра `models`: алиас (ключ словаря) → провайдерская модель + точечные
    оверлеи backend/pricing поверх эффективной секции backend."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    url: str | None = None
    token: str | None = None
    pricing: ModelTariff | None = None
    backend: BackendConfig | None = None

    @model_validator(mode="after")
    def _validate_no_duplicate_settings(self) -> "ModelEntry":
        if (
            self.token is not None
            and self.backend is not None
            and self.backend.chat_completion_headers.get("Authorization") is not None
        ):
            raise ValueError(
                "models.<alias>: token и backend.chat_completion_headers.Authorization "
                "заданы одновременно — оставьте что-то одно"
            )
        if self.url is not None and self.backend is not None and self.backend.completions_url:
            raise ValueError(
                "models.<alias>: url и backend.completions_url — два написания одного "
                "и того же — оставьте что-то одно"
            )
        return self

    @model_validator(mode="after")
    def _validate_connection_pair(self) -> "ModelEntry":
        """§2: своё подключение задаётся ЦЕЛИКОМ. Адрес и токен принадлежат одному
        провайдеру: половина пары либо уносит общий ключ на чужой хост (свой адрес без
        токена), либо уезжает вслед за сменой общего адреса (свой токен без адреса).

        Прокси считается адресом: он определяет, куда физически уходит запрос
        (`connect_backend.resolve_network_params`). `use_ssl`/сертификаты — не адрес, у них
        своё предупреждение (спека §7)."""
        backend = self.backend
        has_address = bool(
            self.url is not None
            or (backend is not None and (backend.completions_url or backend.proxy))
        )
        has_token = bool(
            self.token is not None
            or (backend is not None
                and backend.chat_completion_headers.get("Authorization") is not None)
        )
        if has_address and not has_token:
            raise ValueError(
                'models.<alias>: задан свой адрес, но не задан токен — укажите token '
                '(или token: "" — «сервер не требует авторизации»), либо уберите адрес, '
                "чтобы запись работала через общее подключение"
            )
        if has_token and not has_address:
            raise ValueError(
                "models.<alias>: задан свой токен, но не задан адрес — укажите url, "
                "либо уберите token, чтобы запись работала через общее подключение"
            )
        return self

    @property
    def authorization_header(self) -> str | None:
        """Bearer-нормализация token: None → наследование, "" → явно без авторизации,
        иначе — `Bearer <token>` (уже с префиксом — не дублируется)."""
        if self.token is None or not self.token or self.token.startswith("Bearer "):
            return self.token
        return f"Bearer {self.token}"


class SlotsConfig(BaseModel):
    """Уровни light/default/max — указатели на алиас из `models`."""

    model_config = ConfigDict(extra="forbid")

    light: str | None = None
    default: str | None = None
    max: str | None = None


class AssignConfig(BaseModel):
    """Привязки потребителей → уровень или алиас. Ключ `main` запрещён — основной
    трейс управляется уровнем (`level`/`/level`), не привязкой."""

    model_config = ConfigDict(extra="forbid")

    finish_gate: str | None = None
    text_answer_judge: str | None = None
    command_judge: str | None = None
    summary: str | None = None
    intent_scope: str | None = None
    memory_compaction: str | None = None
    auto_judge: str | None = None
    subagents: dict[str, str] = {}

    @model_validator(mode="before")
    @classmethod
    def _forbid_main(cls, data: Any) -> Any:
        if isinstance(data, dict) and "main" in data:
            raise ValueError(
                "assign.main запрещён — основной трейс управляется уровнем, не привязкой"
            )
        return data
