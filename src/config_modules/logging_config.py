from pydantic import BaseModel, ConfigDict, Field, field_validator


class RedactSecretsConfig(BaseModel):
    """Маскировка секретов в логах (key-name + value-regex). По умолчанию ВЫКЛ."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    extra_keys: list[str] = []       # доп. подстроки имён ключей (к встроенным)
    extra_patterns: list[str] = []   # доп. regex по значению (к встроенным)


class LoggingConfig(BaseModel):
    """Параметры логирования рантайма (не путать со stdlib logging)."""

    model_config = ConfigDict(extra="forbid")

    full_log_backend: bool = False
    console_trace: bool = True
    save_logs: bool = True
    # Формат per-run логов: text (дефолт = историческое поведение) | jsonl.
    format: str = "text"
    # Уровень per-run логгера (актуален для jsonl-режима). На debug добавляются
    # объёмные события backend_request/backend_response/agent_tools.
    log_level: str = "info"
    # Маскировка секретов перед записью в лог (stdout и файл). По умолчанию выкл.
    redact_secrets: RedactSecretsConfig = Field(default_factory=RedactSecretsConfig)

    @field_validator("format")
    @classmethod
    def _validate_format(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"text", "jsonl"}:
            raise ValueError("logging.format должен быть 'text' или 'jsonl'")
        return normalized

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in {"debug", "info", "warning", "error"}:
            raise ValueError(
                "logging.log_level должен быть 'debug', 'info', 'warning' или 'error'"
            )
        return normalized
