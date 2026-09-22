"""Правило «адрес ведёт внутрь машины» и сверка «это один и тот же сервис».

Раньше жило в `voice_runtime.net` рядом с `requests`, сверялось только косвенно —
через `wire.user_voice_conflict` — и потому проверялось лишь на тех формах адреса,
которые встретились установке голоса. После выноса в общий модуль у правила три
потребителя (загрузчик конфига, автозапуск сервиса, голосовой HTTP-путь), и оно
заслуживает прямых сторожей: сверка зовётся из `load_config` на КАЖДОМ старте,
когда прописана локальная батарейка.
"""

import pytest
from gc_core.utils.url_addr import is_loopback, same_service


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8735/v1",
        "http://127.0.0.1:8735/v1",
        "http://127.13.0.9:80/x",  # весь блок 127.* — петля, не только .0.1
        "http://[::1]:8735/v1",
        "http://0.0.0.0:8735/v1",
        "HTTP://LocalHost:8735/v1",  # регистр хоста не имеет значения
        "  http://localhost:8735/v1  ",  # пробелы по краям пришли из config.yml
    ],
)
def test_loopback_forms_people_actually_write(url):
    assert is_loopback(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://api.groq.com/openai/v1/audio/transcriptions",
        "http://localhost.чужой-домен.com:8735/v1",  # начинается на localhost, но чужой
        "http://127.0.0.1.evil.com:8735/v1",
        "",
        "не-адрес-вовсе",
    ],
)
def test_not_loopback(url):
    assert is_loopback(url) is False


def test_same_service_ignores_the_form_of_a_local_address():
    """Человек пишет `localhost`, батарейка — `127.0.0.1`; сервис за ними один."""
    assert same_service("http://localhost:8735/v1", "http://127.0.0.1:8735/v1") is True


def test_different_port_is_a_different_service():
    assert same_service("http://localhost:8735/v1", "http://localhost:9999/v1") is False


def test_remote_address_is_never_the_same_service():
    """Даже при совпадении порта: облачный STT — не локальная батарейка."""
    assert same_service("https://api.groq.com:8735/v1", "http://127.0.0.1:8735/v1") is False


def test_missing_port_is_not_a_match():
    """Без порта сравнивать нечего — считаем «не тот же сервис», а не «тот же»."""
    assert same_service("http://localhost/v1", "http://localhost/v1") is False


def test_broken_port_does_not_raise():
    """Опечатка в порту не имеет права ронять запуск агента.

    `urlsplit(...).port` на «:87x5» бросает ValueError. Без обёртки агент падал сырым
    исключением вместо предупреждения о конфиге (ревью 2026-08-07).
    """
    assert same_service("http://localhost:87x5/v1", "http://127.0.0.1:8x/v1") is False
    assert same_service("http://localhost:87x5/v1", "http://127.0.0.1:8735/v1") is False
