"""Валюта фактической цены выводится из адреса эндпоинта: `.ru` → рубли."""

from usage.currency import provider_currency_for_url

RUB = "₽"
USD = "$"


def test_ru_domain_gives_rubles():
    assert provider_currency_for_url("https://routerai.ru/api/v1/chat/completions") == RUB


def test_non_ru_domain_gives_dollars():
    assert provider_currency_for_url("https://openrouter.ai/api/v1/chat/completions") == USD


def test_subdomains_of_ru():
    assert provider_currency_for_url("https://api.foo.ru/v1/chat/completions") == RUB
    assert provider_currency_for_url("https://a.b.c.ru/v1") == RUB


def test_port_and_userinfo_do_not_break_host():
    assert provider_currency_for_url("https://routerai.ru:8443/api/v1") == RUB
    assert provider_currency_for_url("https://user:pass@routerai.ru/v1") == RUB


def test_case_insensitive():
    assert provider_currency_for_url("HTTPS://RouterAI.RU/API/V1") == RUB


def test_schemeless_url():
    # В конфиг могут вписать адрес без схемы — хост всё равно должен разобраться.
    assert provider_currency_for_url("routerai.ru/api/v1/chat/completions") == RUB


def test_fqdn_trailing_dot():
    assert provider_currency_for_url("https://routerai.ru./api/v1") == RUB


def test_false_positives_stay_dollars():
    # «ru» в других позициях не делает провайдера российским.
    assert provider_currency_for_url("https://ru.example.com/v1") == USD
    assert provider_currency_for_url("https://foo.ru.com/v1") == USD
    assert provider_currency_for_url("https://example.com/ru/v1") == USD
    assert provider_currency_for_url("https://openrouter.ai/api?fallback=foo.ru") == USD
    assert provider_currency_for_url("https://myruprovider.com/v1") == USD
    assert provider_currency_for_url("https://ru/v1") == USD


def test_ip_and_loopback_stay_dollars():
    assert provider_currency_for_url("http://127.0.0.1:8000/v1/chat/completions") == USD
    assert provider_currency_for_url("http://[::1]:8000/v1") == USD
    assert provider_currency_for_url("http://localhost:1234/v1/chat/completions") == USD


def test_garbage_never_raises_and_defaults():
    # В рендере футера падать нельзя: любой мусор — валюта по умолчанию.
    for bad in (None, "", "   ", 123, {}, [], "not a url", "http://[", "://"):
        assert provider_currency_for_url(bad) == USD, bad


def test_default_config_placeholder():
    # Плейсхолдер из config.default.yml не должен притворяться провайдером.
    assert provider_currency_for_url("http://your-backend:PORT/v1/chat/completions") == USD
