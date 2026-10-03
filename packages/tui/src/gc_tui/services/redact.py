"""Маскировка секретов конфига для показа на экране.

Источник правды о секретах — метаданные схемы (``gc_meta(secret=True)`` на поле
модели, ``gc_core.config_modules.field_meta``):

- типизированные настройки (``AppConfig``, экран /config) — ``redact_settings``:
  только метаданные, без угадывания по именам;
- произвольные сырые словари — ``redact_secrets``: известные плоские имена
  ключей (``FLAT_TO_PATH``) маскируются по
  метаданным схемы, а поверх работает эвристика по подстроке имени ключа. Её
  место — только словари, где схемы нет: вычисленные ключи плоского вида
  (``authorization_header`` записи реестра), секции с произвольными ключами,
  чужие сырые документы.
"""

from typing import Any

from gc_core.config_modules.field_meta import (
    meta_for,
    paths_where,
    redact,
    redacted_dump,
)
from gc_core.config_modules.flat_keys import FLAT_TO_PATH
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


def _carries_secrets(path: str) -> bool:
    """Секрет сам путь или что-то под ним (по метаданным схемы)."""
    return meta_for(path).secret or any(
        secret.startswith(path + ".") for secret in paths_where("secret")
    )


def _redact_flat_value(key: str, value: Any) -> Any:
    """Значение плоского ключа — по метаданным его пути в схеме; ключи без
    секретов в схеме возвращаются как есть."""
    path = FLAT_TO_PATH.get(key)
    if path is None or not _carries_secrets(path):
        return value
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    *parent, leaf = path.split(".")
    return redact({leaf: value}, tuple(parent))[leaf]


def redact_secrets(value: Any) -> Any:
    """Рекурсивная копия плоского/сырого value с замаскированными секретами:
    известные плоские ключи — по метаданным схемы, затем эвристика по имени.
    Не мутирует вход."""
    if isinstance(value, dict):
        value = {k: _redact_flat_value(str(k), v) for k, v in value.items()}
    return _redact_by_name(value)


def redact_settings(settings: BaseModel) -> dict[str, Any]:
    """Дамп типизированных настроек для показа: секреты — по метаданным схемы."""
    return redacted_dump(settings)
