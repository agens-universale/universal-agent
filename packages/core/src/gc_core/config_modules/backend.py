from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

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


class BackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_mode: str = "responses"
    completions_url: str = ""
    chat_completions_max_tokens: int = 32768
    chat_completions_min_response_tokens: int = 1024
    chat_completion_headers: dict[str, str] = {}
    chat_completion_kwargs: dict[str, Any] = {}
    use_max_tokens_param: bool = False
    openai_strict_tools_schema: bool = False
    send_tool_strict: bool = True
    # Разметка prompt cache в запросах главного цикла: не задано → авто по id модели
    # (подстрока claude); true/false — принудительно включить/выключить (в т.ч. на
    # одну модель через models.<alias>.backend).
    claude_prompt_cache: bool | None = None
    use_ssl: bool | str = True
    cert_path: str | None = None
    key_path: str | None = None
    use_retry: bool = False
    retry_embedded_choice_errors: bool = True
    # HTTP-таймаут одного запроса к LLM (connect + idle между чанками при stream).
    request_timeout_sec: int = DEFAULT_REQUEST_TIMEOUT_SEC
    stream: bool = True
    # HTTP/SOCKS-прокси для backend-вызовов. Пусто → из env (HTTPS_PROXY/HTTP_PROXY/ALL_PROXY).
    proxy: str | None = None

    @field_validator("api_mode")
    @classmethod
    def validate_api_mode(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"responses", "chat_completions"}:
            raise ValueError("backend.api_mode должен быть 'responses' или 'chat_completions'")
        return normalized

    @field_validator("chat_completions_max_tokens", mode="before")
    @classmethod
    def normalize_tokens(cls, value: Any) -> int:
        return parse_chat_completions_max_tokens(value, default=8096)

    @field_validator("chat_completions_min_response_tokens", mode="before")
    @classmethod
    def normalize_min_response_tokens(cls, value: Any) -> int:
        return parse_chat_completions_max_tokens(value, default=1024)

    @field_validator("request_timeout_sec", mode="before")
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

    @field_validator("chat_completion_kwargs", mode="before")
    @classmethod
    def normalize_chat_completion_kwargs(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        return {}

    @field_validator("chat_completion_headers", mode="before")
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

    @field_validator("proxy", mode="before")
    @classmethod
    def normalize_proxy(cls, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return None
