"""Сессионный кэш списков моделей (спека умных форм §3.2): ключ —
(url, auth, verify, proxy), живёт процесс TUI. Ошибка/None не кэшируется —
следующий вход в поле пробует снова."""

from __future__ import annotations

_CACHE: dict = {}


def clear_cache() -> None:
    _CACHE.clear()


def make_cached_fetch(fetch):
    def cached(url, auth, verify=True, config_proxy=None, refresh=False):
        """`refresh` — принудительно сходить на сервер мимо кэша (Alt+↓, спека §5.1
        «идём на сервер за списком»; роль ушла с Ctrl+Space — спека 2026-08-06 §2,
        §4.1). Без него клавиша была бы ДЕТЕРМИНИРОВАННЫМ
        no-op: ключ кэша заморожен в конструкторе `ModelEditor` и за жизнь редактора
        не меняется — тот же ключ отдавал бы тот же ответ всегда.

        Неудача ключ НЕ гасит (`if models:` — то же правило, что и при обычном
        промахе): иначе «сервер моргнул» стёр бы нормальный список, и следующий вход
        в поле пошёл бы в сеть за тем же самым."""
        key = (url, auth, verify, config_proxy)
        if not refresh and key in _CACHE:
            return _CACHE[key]
        models = fetch(url, auth, verify=verify, config_proxy=config_proxy)
        # Кортеж (models, payload) из каталога формы — кэшируем по списку id.
        cacheable = models[0] if isinstance(models, tuple) else models
        if cacheable:
            _CACHE[key] = models
        return models

    return cached
