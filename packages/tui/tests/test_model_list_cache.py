from gc_tui.services.model_list_cache import (
    clear_cache,
    make_cached_catalog_fetch,
    make_cached_fetch,
)


def test_second_call_same_key_hits_cache():
    clear_cache()
    calls = []

    def fetch(url, auth, verify=True, config_proxy=None):
        calls.append(url)
        return [{"id": "m1"}]

    cached = make_cached_fetch(fetch)
    assert cached("http://a", "tok") == [{"id": "m1"}]
    assert cached("http://a", "tok") == [{"id": "m1"}]
    assert calls == ["http://a"]


def test_key_includes_url_auth_proxy():
    clear_cache()
    calls = []

    def fetch(url, auth, verify=True, config_proxy=None):
        calls.append((url, auth, config_proxy))
        return [1]

    cached = make_cached_fetch(fetch)
    cached("http://a", "t1")
    cached("http://a", "t2")
    cached("http://a", "t1", config_proxy="socks5h://p")
    assert len(calls) == 3


def test_error_not_cached():
    clear_cache()
    state = {"fail": True}

    def fetch(url, auth, verify=True, config_proxy=None):
        if state["fail"]:
            raise OSError("down")
        return [1]

    cached = make_cached_fetch(fetch)
    try:
        cached("http://a", None)
    except OSError:
        pass
    state["fail"] = False
    assert cached("http://a", None) == [1]


def test_none_result_not_cached():
    clear_cache()
    results = [None, [1]]

    def fetch(url, auth, verify=True, config_proxy=None):
        return results.pop(0)

    cached = make_cached_fetch(fetch)
    assert cached("http://a", None) is None
    assert cached("http://a", None) == [1]


# --- refresh: принудительное перечитывание мимо кэша (§5.1, гейт §11.5) ---


def test_refresh_bypasses_the_cache_and_rewrites_the_key():
    """Alt+↓ обязан СХОДИТЬ на сервер, а не вернуть кэш: ключ кэша
    (url, auth, verify, proxy) заморожен в конструкторе ModelEditor и за жизнь
    редактора не меняется — без обхода клавиша была бы детерминированным no-op.
    Свежий ответ ложится в тот же ключ, иначе следующий обычный вызов вернул бы
    только что опровергнутый список."""
    clear_cache()
    results = [[1], [1, 2]]
    calls = []

    def fetch(url, auth, verify=True, config_proxy=None):
        calls.append(url)
        return results.pop(0)

    cached = make_cached_fetch(fetch)
    assert cached("http://a", None) == [1]
    assert (
        cached("http://a", None) == [1] and len(calls) == 1
    )  # обычный вызов — кэш
    assert cached("http://a", None, refresh=True) == [1, 2]
    assert len(calls) == 2, "refresh обязан сходить на сервер"
    assert cached("http://a", None) == [1, 2], (
        "свежий ответ должен лечь в ключ"
    )


def test_failed_refresh_keeps_the_cached_list():
    """Неудачное перечитывание ключ НЕ гасит: иначе «сервер моргнул» стёр бы
    нормальный список, и следующий вход в поле пошёл бы в сеть за тем же самым."""
    clear_cache()
    results = [[1], None]

    def fetch(url, auth, verify=True, config_proxy=None):
        return results.pop(0)

    cached = make_cached_fetch(fetch)
    assert cached("http://a", None) == [1]
    assert cached("http://a", None, refresh=True) is None  # сервер не ответил
    assert cached("http://a", None) == [1], "кэш затёрт неудачей"


# --- каталог формы записи: своё хранилище, общий с редакторами ключ ---


def _catalog_fetch(calls):
    def fetch(url, auth, verify=True, config_proxy=None):
        calls.append("catalog")
        return [{"id": "m1"}], {"data": [{"id": "m1", "context_length": 8}]}

    return fetch


def _list_fetch(calls):
    def fetch(url, auth, verify=True, config_proxy=None):
        calls.append("list")
        return [{"id": "m1"}]

    return fetch


def test_catalog_does_not_leak_into_the_list_cache():
    """Форма записи кэширует пару (список, тело ответа), а `ModelEditor` и мастер
    по тому же ключу (url, auth, verify, proxy) ждут СПИСОК: общий словарь отдавал
    им кортеж, и `{m.id for m in models}` падал на первом же входе в редактор."""
    clear_cache()
    calls = []
    catalog = make_cached_catalog_fetch(_catalog_fetch(calls))
    listing = make_cached_fetch(_list_fetch(calls))
    catalog("http://a", "tok")
    assert listing("http://a", "tok") == [{"id": "m1"}]
    assert calls == ["catalog", "list"]


def test_list_does_not_leak_into_the_catalog_cache():
    """Обратный порядок: список без тела ответа оставил бы форму без карточки
    модели до ручного перечитывания."""
    clear_cache()
    calls = []
    listing = make_cached_fetch(_list_fetch(calls))
    catalog = make_cached_catalog_fetch(_catalog_fetch(calls))
    listing("http://a", "tok")
    models, payload = catalog("http://a", "tok")
    assert models == [{"id": "m1"}]
    assert payload == {"data": [{"id": "m1", "context_length": 8}]}
    assert calls == ["list", "catalog"]


def test_catalog_second_call_hits_cache_and_refresh_bypasses_it():
    clear_cache()
    calls = []
    catalog = make_cached_catalog_fetch(_catalog_fetch(calls))
    first = catalog("http://a", "tok")
    assert catalog("http://a", "tok") == first
    assert calls == ["catalog"]
    catalog("http://a", "tok", refresh=True)
    assert calls == ["catalog", "catalog"]


def test_empty_catalog_is_not_cached():
    """Правило то же, что у списка: пустой ответ ключ не занимает."""
    clear_cache()
    results = [([], {"data": []}), ([{"id": "m1"}], {"data": [{"id": "m1"}]})]

    def fetch(url, auth, verify=True, config_proxy=None):
        return results.pop(0)

    catalog = make_cached_catalog_fetch(fetch)
    assert catalog("http://a", None) == ([], {"data": []})
    assert catalog("http://a", None)[0] == [{"id": "m1"}]


def test_clear_cache_drops_the_catalog_too():
    clear_cache()
    calls = []
    catalog = make_cached_catalog_fetch(_catalog_fetch(calls))
    catalog("http://a", "tok")
    clear_cache()
    catalog("http://a", "tok")
    assert calls == ["catalog", "catalog"]
