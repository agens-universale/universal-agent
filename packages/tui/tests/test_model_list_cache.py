from gc_tui.services.model_list_cache import _CACHE, make_cached_fetch


def test_second_call_same_key_hits_cache():
    _CACHE.clear()
    calls = []

    def fetch(url, auth, verify=True, config_proxy=None):
        calls.append(url)
        return [{"id": "m1"}]

    cached = make_cached_fetch(fetch)
    assert cached("http://a", "tok") == [{"id": "m1"}]
    assert cached("http://a", "tok") == [{"id": "m1"}]
    assert calls == ["http://a"]


def test_key_includes_url_auth_proxy():
    _CACHE.clear()
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
    _CACHE.clear()
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
    _CACHE.clear()
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
    _CACHE.clear()
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
    _CACHE.clear()
    results = [[1], None]

    def fetch(url, auth, verify=True, config_proxy=None):
        return results.pop(0)

    cached = make_cached_fetch(fetch)
    assert cached("http://a", None) == [1]
    assert cached("http://a", None, refresh=True) is None  # сервер не ответил
    assert cached("http://a", None) == [1], "кэш затёрт неудачей"
