"""TUI подсказывает Textual truecolor на терминалах, где он точно есть
(Windows Terminal / VS Code / COLORTERM) — иначе авто-детект может выбрать 256
и градиент логотипа рендерится ступеньками (Windows)."""

import os

import gc_tui as tui


def _clear(monkeypatch):
    for v in (
        "TEXTUAL_COLOR_SYSTEM",
        "WT_SESSION",
        "TERM_PROGRAM",
        "COLORTERM",
    ):
        monkeypatch.delenv(v, raising=False)


def test_forces_on_windows_terminal(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("WT_SESSION", "x")
    tui._force_truecolor_if_supported()
    assert os.environ["TEXTUAL_COLOR_SYSTEM"] == "truecolor"


def test_forces_on_vscode(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    tui._force_truecolor_if_supported()
    assert os.environ["TEXTUAL_COLOR_SYSTEM"] == "truecolor"


def test_forces_on_colorterm(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("COLORTERM", "truecolor")
    tui._force_truecolor_if_supported()
    assert os.environ["TEXTUAL_COLOR_SYSTEM"] == "truecolor"


def test_no_force_without_markers(monkeypatch):
    _clear(monkeypatch)
    tui._force_truecolor_if_supported()
    assert "TEXTUAL_COLOR_SYSTEM" not in os.environ


def test_does_not_override_explicit(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("WT_SESSION", "x")
    monkeypatch.setenv(
        "TEXTUAL_COLOR_SYSTEM", "256"
    )  # явный выбор пользователя
    tui._force_truecolor_if_supported()
    assert os.environ["TEXTUAL_COLOR_SYSTEM"] == "256"
