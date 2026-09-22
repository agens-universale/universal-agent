"""TDD-тест: флаг --init-config существует в arg_parser (ренейм с --init)."""

import sys

import pytest


def _parse(argv, monkeypatch):
    """Прямой парс без subprocess — надёжнее в среде проекта.

    Подменяем sys.argv и перезагружаем arg_parser, чтобы получить свежий
    parsed_args — модуль парсит аргументы на импорте.
    """
    monkeypatch.setattr(sys, "argv", argv)
    import importlib

    from gc_cli import arg_parser as ap_mod

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
        importlib.reload(importlib.import_module("gc_cli.arg_parser"))


def test_trace_id_both_spellings_are_the_same_flag(monkeypatch):
    """--trace-id и --trace_id — один параметр, оба идут в dest trace_id."""
    dash = _parse(["gc-cli", "-q", "x", "--trace-id", "wf-42"], monkeypatch)
    assert dash.trace_id == "wf-42"
    underscore = _parse(["gc-cli", "-q", "x", "--trace_id", "wf-42"], monkeypatch)
    assert underscore.trace_id == "wf-42"


def test_trace_id_underscore_is_not_a_generic_override(monkeypatch):
    """Подчёркнутая форма объявлена, поэтому НЕ попадает в generic-оверрайды.

    Иначе значение прошло бы через yaml.safe_load: `00123` → 83, `on` → True.
    """
    monkeypatch.setattr(sys, "argv", ["gc-cli", "-q", "x", "--trace_id", "00123"])
    import importlib

    from gc_cli import arg_parser as ap_mod

    importlib.reload(ap_mod)
    assert ap_mod.run_args.trace_id == "00123"  # строка, не 83
    assert "trace_id" not in ap_mod.extra_config_overrides
