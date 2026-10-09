"""Сессионный кэш списков моделей (спека умных форм §3.2): ключ —
(url, auth, verify, proxy), живёт процесс TUI. Ошибка/None не кэшируется —
следующий вход в поле пробует снова."""

from __future__ import annotations

_CACHE: dict = {}
# Каталог формы записи — пара (список, сырое тело ответа). Хранилище своё: ключ у
# него тот же, что у списка, а `ModelEditor` и мастер ждут по нему голый список.
_CATALOG_CACHE: dict = {}


def clear_cache() -> None:
    _CACHE.clear()
    _CATALOG_CACHE.clear()


def make_cached_fetch(fetch):
    return _make_cached(fetch, _CACHE, lambda models: models)


def make_cached_catalog_fetch(fetch):
    """То же для `fetch`, отдающего пару `(models, payload)`: правила кэша общие,
    непустота судится по списку."""
    return _make_cached(fetch, _CATALOG_CACHE, lambda catalog: catalog[0])


def _make_cached(fetch, store: dict, models_of):
    def cached(url, auth, verify=True, config_proxy=None, refresh=False):
        """`refresh` — принудительно сходить на сервер мимо кэша (Alt+↓, спека §5.1
        «идём на сервер за списком»; роль ушла с Ctrl+Space — спека 2026-08-06 §2,
        §4.1). Без него клавиша была бы ДЕТЕРМИНИРОВАННЫМ
        no-op: ключ кэша заморожен в конструкторе `ModelEditor` и за жизнь редактора
        не меняется — тот же ключ отдавал бы тот же ответ всегда.

        Неудача ключ НЕ гасит (проверка непустоты — то же правило, что и при обычном
        промахе): иначе «сервер моргнул» стёр бы нормальный список, и следующий вход
        в поле пошёл бы в сеть за тем же самым."""
        key = (url, auth, verify, config_proxy)
        if not refresh and key in store:
            return store[key]
        result = fetch(url, auth, verify=verify, config_proxy=config_proxy)
        if models_of(result):
            store[key] = result
        return result

    return cached
