"""Настройки и контексты для тестов — без плоского словаря в коде агента.

Тест описывает конфиг как удобно: прежними плоскими именами (``max_steps``,
``write_guard``) и/или путями (``agent.max_steps``). Коду агента уходят
настоящие ``AppConfig``/``RuntimeEnv`` — строго провалидированные, как из файла.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping, Optional

from gc_core.settings import LoadedSettings, settings_from_flat


def make_settings(
    config: Optional[Mapping[str, Any]] = None, **dotted: Any
) -> LoadedSettings:
    """``LoadedSettings`` из словаря теста: плоские имена и пути через точку.

    ``make_settings({"max_steps": 3})``, ``make_settings(**{"agent.max_steps": 3})``.
    Рантайм-ключи (``launch_root``, ``trace_id``, ``profile``…) уходят в
    ``RuntimeEnv``. Неизвестный ключ или негодное значение — ``ValidationError``.
    """
    return settings_from_flat({**dict(config or {}), **dotted})


def fake_ctx(
    config: Optional[Mapping[str, Any]] = None,
    *,
    loaded: Optional[LoadedSettings] = None,
    **attrs: Any,
) -> SimpleNamespace:
    """Минимальный контекст рана для тестов: ``settings``, ``runtime`` и любые
    дополнительные атрибуты (``logger=…``, ``os_backend=…``)."""
    from gc_core.config import flat_config

    loaded = loaded if loaded is not None else make_settings(config)
    attrs.setdefault("settings", loaded.settings)
    attrs.setdefault("runtime", loaded.runtime)
    # Переходно: плоский вид для кода, ещё читающего ctx.config_dict.
    attrs.setdefault("config_dict", flat_config(loaded))
    return SimpleNamespace(**attrs)


__all__ = ["fake_ctx", "make_settings"]
