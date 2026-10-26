import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gc_core.config_modules.field_meta import gc_meta


class RedactSecretsConfig(BaseModel):
    """Маскировка секретов в логах (key-name + value-regex). По умолчанию ВЫКЛ."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    extra_keys: list[str] = []  # доп. подстроки имён ключей (к встроенным)
    extra_patterns: list[str] = []  # доп. regex по значению (к встроенным)

    @field_validator("extra_patterns")
    @classmethod
    def validate_extra_patterns(cls, value: list[str]) -> list[str]:
        """Негодный regex — ошибка схемы, а не warning в лог: маскировщик
        (`security/log_redaction.py`) молча пропускал такой шаблон, и секрет,
        который человек собирался скрыть, уезжал в лог открытым текстом."""
        bad = []
        for i, pattern in enumerate(value):
            try:
                re.compile(str(pattern))
            except re.error as exc:
                bad.append(f"[{i}] {pattern!r}: {exc}")
        if bad:
            raise ValueError(
                "негодное регулярное выражение — " + "; ".join(bad)
            )
        return value


class LoggingConfig(BaseModel):
    """Параметры логирования рантайма (не путать со stdlib logging)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    full_log_backend: bool = False
    console_trace: bool = True
    save_logs: bool = True
    # Формат per-run логов: text (дефолт = историческое поведение) | jsonl.
    format: str = "text"
    # Уровень per-run логгера (актуален для jsonl-режима). На debug добавляются
    # объёмные события backend_request/backend_response/agent_tools.
    log_level: str = "info"
    # Всплывать ли записям в корневой логгер приложения.
    #
    # Дефолт false — историческое поведение: ран пишет в свой файл и не
    # засоряет чужие хендлеры. Встраивающему, который настроил logging по-своему
    # и хочет видеть жизнь агента у себя, нужен true: без него он не увидит
    # НИЧЕГО, потому что per-run логгер обрывает цепочку.
    propagate: bool = False
    # Маскировка секретов перед записью в лог (stdout и файл). По умолчанию выкл.
    redact_secrets: RedactSecretsConfig = Field(
        default_factory=RedactSecretsConfig,
        json_schema_extra=gc_meta(expert=True),
    )

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
