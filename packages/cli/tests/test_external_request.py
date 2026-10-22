"""`--external-request`: запуск по запросу внешней системы (swarm).

Канал объявляет о себе так же, как webhook сервера: спросить некого, а слова
запроса для судьи действий — запрос внешней системы, не разрешение человека.
"""

import importlib
import sys

from gc_testkit.settings import make_settings


def _load(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", argv)
    from gc_cli import arg_parser

    importlib.reload(arg_parser)
    from gc_cli import cli

    monkeypatch.setattr(
        cli, "load_settings", lambda *_a, **_k: make_settings()
    )
    loaded, code = cli._load_cli_settings(None, None)
    assert code is None
    return loaded.runtime


def test_external_request_declares_the_channel(monkeypatch):
    runtime = _load(["gc-cli", "-q", "{}", "--external-request"], monkeypatch)
    assert runtime.human_channel is False
    assert runtime.external_user_words is True


def test_without_the_flag_the_channel_is_detected_as_before(monkeypatch):
    runtime = _load(["gc-cli", "-q", "x"], monkeypatch)
    assert runtime.human_channel is None
    assert runtime.external_user_words is False
