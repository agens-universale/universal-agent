"""TDD-тест: флаг --init-config существует в arg_parser (ренейм с --init)."""
import sys
import types

import pytest


def _parse(argv, monkeypatch):
    """Прямой парс без subprocess — надёжнее в среде проекта.

    Подменяем sys.argv и перезагружаем arg_parser, чтобы получить свежий
    parsed_args — модуль парсит аргументы на импорте.
    """
    monkeypatch.setattr(sys, "argv", argv)
    import importlib
    import arg_parser as ap_mod
    importlib.reload(ap_mod)
    return ap_mod.run_args


def test_init_config_flag_exists(monkeypatch):
    """--init-config принимается и ставит атрибут init_config=True."""
    run_args = _parse(["gc-cli", "--init-config"], monkeypatch)
    assert getattr(run_args, "init_config", False) is True


def test_init_config_false_when_absent(monkeypatch):
    """При обычном запуске с -q атрибут init_config=False."""
    run_args = _parse(["gc-cli", "-q", "привет"], monkeypatch)
    assert getattr(run_args, "init_config", None) is False


def test_init_flag_registered(monkeypatch):
    """--init зарегистрирован как флаг генерации AGENTS.md (Task 8)."""
    run_args = _parse(["gc-cli", "--init"], monkeypatch)
    assert getattr(run_args, "init", False) is True


def test_init_config_and_query_mutually_exclusive(monkeypatch):
    """--init-config и -q вместе должны вызвать SystemExit."""
    import importlib
    monkeypatch.setattr(sys, "argv", ["gc-cli", "--init-config", "-q", "hi"])
    with pytest.raises(SystemExit):
        importlib.reload(importlib.import_module("arg_parser"))
