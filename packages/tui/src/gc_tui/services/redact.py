"""Маскировка секретов конфига для показа на экране.

Источник правды о секретах — метаданные схемы (``gc_meta(secret=True)`` на поле
модели, ``gc_core.config_modules.field_meta``):

- типизированные настройки (``AppConfig``, экран /config) — ``redact_settings``:
  только метаданные, без угадывания по именам;
- произвольные сырые словари — ``redact_secrets``: эвристика по подстроке
  имени ключа. Её место — только словари, где схемы нет: вычисленные ключи
  (``authorization_header`` записи реестра), секции с произвольными ключами,
  чужие сырые документы.
"""

from typing import Any

from gc_core.config_modules.field_meta import redacted_dump
from pydantic import BaseModel

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


def _redact_by_name(value: Any) -> Any:
    """Эвристика: маскировать значения ключей с секрет-подобным именем."""
    if isinstance(value, dict):
        return {
            k: (
                _MASK
                if (_is_secret_key(str(k)) and v not in (None, "", {}, []))
                else _redact_by_name(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_by_name(v) for v in value]
    return value


def redact_secrets(value: Any) -> Any:
    """Рекурсивная копия сырого value с замаскированными секретами — эвристика
    по имени ключа (схемы у сырого словаря нет). Не мутирует вход."""
    return _redact_by_name(value)


def redact_settings(settings: BaseModel) -> dict[str, Any]:
    """Дамп типизированных настроек для показа: секреты — по метаданным схемы."""
    return redacted_dump(settings)
