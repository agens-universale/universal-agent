from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


def parse_chat_completions_max_tokens(raw: Any, *, default: int = 8096) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        n = int(raw)
        return n if n >= 1 else default
    if isinstance(raw, str):
        s = raw.strip().lower().replace(" ", "").replace("_", "")
        if not s:
            return default
        if s.endswith("k"):
            try:
                base = float(s[:-1])
                n = int(base * 1000)
            except ValueError:
                return default
            return n if n >= 1 else default
        try:
            n = int(float(s))
        except ValueError:
            return default
        return n if n >= 1 else default
    return default


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
    use_ssl: bool = True
    use_retry: bool = False
    stream: bool = True
    stream_answer_preview: bool = False

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
