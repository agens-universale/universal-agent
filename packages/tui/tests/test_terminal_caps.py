"""Выбор глифа кнопки по терминалу: подстановка шрифтов есть — берём `⧉`, нет — `❐`."""

from __future__ import annotations

import pytest
from gc_tui.services import terminal_caps
from gc_tui.services.terminal_caps import supports_font_fallback
from gc_tui.widgets.block_panel import COPY_GLYPH_RICH, COPY_GLYPH_SAFE, copy_glyph


def _windows_console(monkeypatch, **env):
    """Старая консоль Windows: nt-платформа без признаков богатого терминала."""
    monkeypatch.setattr(terminal_caps, "_os_name", lambda: "nt")
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_conhost_has_no_font_fallback(monkeypatch):
    _windows_console(monkeypatch)
    assert supports_font_fallback() is False


def test_windows_terminal_has_font_fallback(monkeypatch):
    _windows_console(monkeypatch, WT_SESSION="a-b-c")
    assert supports_font_fallback() is True


def test_vscode_terminal_has_font_fallback(monkeypatch):
    _windows_console(monkeypatch, TERM_PROGRAM="vscode")
    assert supports_font_fallback() is True


def _posix(monkeypatch, **env):
    monkeypatch.setattr(terminal_caps, "_os_name", lambda: "posix")
    for key in ("WT_SESSION", "TERM_PROGRAM", "WSL_DISTRO_NAME", "WSL_INTEROP"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_non_windows_has_font_fallback(monkeypatch):
    _posix(monkeypatch)
    assert supports_font_fallback() is True


def test_wsl_without_windows_terminal_is_treated_as_console(monkeypatch):
    """WSL — posix, но рисует терминал Windows; какой именно, мы не знаем.

    Цена ошибки несимметрична: лишний `❐` безобиден, лишний `⧉` — пустой
    прямоугольник. Поэтому без явного признака Windows Terminal отвечаем False.
    """
    _posix(monkeypatch, WSL_DISTRO_NAME="Ubuntu")
    assert supports_font_fallback() is False

    _posix(monkeypatch, WSL_INTEROP="/run/WSL/1_interop")
    assert supports_font_fallback() is False


def test_wsl_inside_windows_terminal_gets_rich_glyph(monkeypatch):
    _posix(monkeypatch, WSL_DISTRO_NAME="Ubuntu", WT_SESSION="a-b-c")
    assert supports_font_fallback() is True


def test_glyph_follows_terminal_capability(monkeypatch):
    _windows_console(monkeypatch)
    assert copy_glyph() == COPY_GLYPH_SAFE

    _windows_console(monkeypatch, WT_SESSION="a-b-c")
    assert copy_glyph() == COPY_GLYPH_RICH


def test_both_glyphs_are_single_column():
    """Ширина обоих — одна колонка: смена глифа не должна двигать вёрстку шапки."""
    from rich.cells import cell_len

    assert cell_len(COPY_GLYPH_RICH) == 1
    assert cell_len(COPY_GLYPH_SAFE) == 1


@pytest.mark.asyncio
async def test_button_shows_glyph_for_current_terminal(tui_app_factory):
    """Виджет берёт глиф через copy_glyph(), а не фиксированную константу."""
    from gc_tui.widgets.block_panel import BlockPanel
    from textual.widgets import Static

    app = tui_app_factory(BlockPanel.for_code("x = 1", "python", event_no=1))
    async with app.run_test(size=(60, 12)):
        assert app.query_one(".block-copy", Static).visual.plain == copy_glyph()
