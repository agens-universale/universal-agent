from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    create_model,
    field_validator,
)

from gc_core.config_modules.field_meta import gc_meta, masked_repr_args

DEFAULT_REQUEST_TIMEOUT_SEC = 600


def parse_chat_completions_max_tokens(raw: Any, *, default: int = 8096) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        n = int(raw)
        return n if n >= 1 else default
    if isinstance(raw, str):
        return _parse_max_tokens_str(raw, default)
    return default


def _parse_max_tokens_str(raw: str, default: int) -> int:
    """Строковая форма: ``"8192"``, ``"32_768"``, ``"16k"``; иначе/≤0 → default."""
    s = raw.strip().lower().replace(" ", "").replace("_", "")
    if not s:
        return default
    try:
        if s.endswith("k"):
            n = int(float(s[:-1]) * 1000)
        else:
            n = int(float(s))
    except ValueError:
        return default
    return n if n >= 1 else default


class _BackendNormalizers:
    """Нормализация полей backend — общая для секции ``backend`` и её
    переопределения на модели (``models.<alias>.backend``)."""

    @field_validator(
        "chat_completions_max_tokens", mode="before", check_fields=False
    )
    @classmethod
    def normalize_tokens(cls, value: Any) -> int:
        return parse_chat_completions_max_tokens(value, default=8096)

    @field_validator(
        "chat_completions_min_response_tokens",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def normalize_min_response_tokens(cls, value: Any) -> int:
        return parse_chat_completions_max_tokens(value, default=1024)

    @field_validator("request_timeout_sec", mode="before", check_fields=False)
    @classmethod
    def normalize_request_timeout_sec(cls, value: Any) -> int:
        if value is None:
            return DEFAULT_REQUEST_TIMEOUT_SEC
        try:
            n = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "backend.request_timeout_sec должен быть положительным целым числом"
            ) from exc
        if n <= 0:
            raise ValueError(
                "backend.request_timeout_sec должен быть положительным целым числом"
            )
        return n

    @field_validator(
        "chat_completion_kwargs", mode="before", check_fields=False
    )
    @classmethod
    def normalize_chat_completion_kwargs(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        return {}

    @field_validator(
        "chat_completion_headers", mode="before", check_fields=False
    )
    @classmethod
    def normalize_chat_completion_headers(cls, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            return {}
        normalized: dict[str, str] = {}
        for key, raw_val in value.items():
            if not isinstance(key, str):
                continue
            key_clean = key.strip()
            if not key_clean:
                continue
            if raw_val is None:
                continue
            val_clean = str(raw_val).strip()
            if not val_clean:
                continue
            normalized[key_clean] = val_clean
        return normalized

    @field_validator("proxy", mode="before", check_fields=False)
    @classmethod
    def normalize_proxy(cls, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return None


class BackendConfig(_BackendNormalizers, BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_mode: str = "responses"
    completions_url: str = Field(
        default="", json_schema_extra=gc_meta(worker_only=True)
    )
    chat_completions_max_tokens: int = 32768
    chat_completions_min_response_tokens: int = Field(
        default=1024, json_schema_extra=gc_meta(expert=True)
    )
    # Секрет-словарь: ключи (имена заголовков) видны, значения маскируются.
    chat_completion_headers: dict[str, str] = Field(
        default={}, json_schema_extra=gc_meta(secret=True, worker_only=True)
    )
    chat_completion_kwargs: dict[str, Any] = Field(
        default={}, json_schema_extra=gc_meta(expert=True)
    )
    use_max_tokens_param: bool = False
    openai_strict_tools_schema: bool = False
    send_tool_strict: bool = True
    # Разметка prompt cache в запросах главного цикла: не задано → авто по id модели
    # (подстрока claude); true/false — принудительно включить/выключить (в т.ч. на
    # одну модель через models.<alias>.backend).
    claude_prompt_cache: bool | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True)
    )
    use_ssl: bool | str = Field(
        default=True, json_schema_extra=gc_meta(worker_only=True)
    )
    cert_path: str | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True, worker_only=True)
    )
    key_path: str | None = Field(
        default=None, json_schema_extra=gc_meta(expert=True, worker_only=True)
    )
    use_retry: bool = False
    retry_embedded_choice_errors: bool = Field(
        default=True, json_schema_extra=gc_meta(expert=True)
    )
    # HTTP-таймаут одного запроса к LLM (connect + idle между чанками при stream).
    request_timeout_sec: int = DEFAULT_REQUEST_TIMEOUT_SEC
    stream: bool = Field(default=True, json_schema_extra=gc_meta(expert=True))
    # HTTP/SOCKS-прокси для backend-вызовов. Пусто → из env (HTTPS_PROXY/HTTP_PROXY/ALL_PROXY).
    # Секрет: адрес прокси может нести учётные данные (http://user:pass@host).
    proxy: str | None = Field(
        default=None, json_schema_extra=gc_meta(secret=True, worker_only=True)
    )

    def __repr_args__(self):
        return masked_repr_args(self)

    @field_validator("api_mode")
    @classmethod
    def validate_api_mode(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"responses", "chat_completions"}:
            raise ValueError(
                "backend.api_mode должен быть 'responses' или 'chat_completions'"
            )
        return normalized


#: Поля backend, которые на уровне модели не применяются: протокол и режим
#: потока выбираются для всего бэкенда, строгость схем тулов — тоже. В
#: ``models.<alias>.backend`` их нет: лишний ключ в YAML — предупреждение.
MODEL_LEVEL_UNSUPPORTED = frozenset(
    {"api_mode", "openai_strict_tools_schema", "send_tool_strict", "stream"}
)


class _ModelBackendBase(_BackendNormalizers, BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def __repr_args__(self):
        return masked_repr_args(self)


#: Переопределения backend на одной модели — поля ``BackendConfig`` без
#: ``MODEL_LEVEL_UNSUPPORTED``. Собирается из полей ``BackendConfig``, чтобы
#: умолчания и метаданные не расходились.
ModelBackendConfig = create_model(
    "ModelBackendConfig",
    __base__=_ModelBackendBase,
    __doc__="Переопределения backend на модели (models.<alias>.backend).",
    **{
        name: (info.annotation, info)
        for name, info in BackendConfig.model_fields.items()
        if name not in MODEL_LEVEL_UNSUPPORTED
    },
)
