"""Маскировка секрет-подобных значений конфига для показа на экране."""

from typing import Any

# Важно: под ключи конфига, отображаемые в TUI.
# Содержит фрагменты, которые должны совпадать без учёта регистра.
_SECRET_SUBSTRINGS = (
    "token",
    "api_key",
    "api-key",
    "authorization",
    "secret",
    "password",
)
_MASK = "***"


def _is_secret_key(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in _SECRET_SUBSTRINGS)


def redact_secrets(value: Any) -> Any:
    """Рекурсивная копия value с замаскированными секрет-ключами. Не мутирует вход."""
    if isinstance(value, dict):
        return {
            k: (
                _MASK
                if (_is_secret_key(str(k)) and v not in (None, "", {}, []))
                else redact_secrets(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(v) for v in value]
    return value
