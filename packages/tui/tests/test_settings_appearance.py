"""SettingsScreen, секция «Внешний вид»: строка темы `tui.theme`."""

from typing import get_args

import pytest
import yaml

pytest.importorskip("textual")

from gc_core.config_modules.tui import TuiConfig
from gc_testkit.tui_support import open_settings_section, tui_settings
from textual.widgets import Static


def _app_with_config(tmp_path, yaml_text):
    from gc_tui import app as app_mod

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(yaml_text, encoding="utf-8")
    app = app_mod.TuiApp(settings=tui_settings({}))
    app._config_path = str(cfg_file)
    app.reload_config()
    return app, cfg_file


def _screen(app):
    from gc_tui.screens.settings import SettingsScreen

    return SettingsScreen(
        settings=app.base_settings(),
        config_path=app._config_path,
        on_apply=app.reload_config,
    )


async def _open_look(pilot):
    scr = _screen(pilot.app)
    pilot.app.push_screen(scr)
    await pilot.pause()
    open_settings_section(scr, "Внешний вид")
    await pilot.pause(0.1)
    return scr


def _texts(scr, selector=Static):
    return [str(w.render()) for w in scr.query(selector)]


_AUTO_HINT = (
    "Подстраивается под фон вашего терминала и проверяет контраст текста. "
    "Если он слишком низкий, включается тема GigaCode."
)
_GIGACODE_HINT = "Фирменная тёмная тема. Выглядит одинаково в любом терминале."


def _theme_in_file(cfg_file):
    return yaml.safe_load(cfg_file.read_text(encoding="utf-8"))["tui"]["theme"]


def test_look_section_follows_work_modes():
    from gc_tui.screens.settings import SECTIONS

    assert SECTIONS.index("Внешний вид") == (
        SECTIONS.index("Режимы работы") + 1
    )


def test_every_schema_theme_has_a_name():
    from gc_tui.screens.settings import THEME_NAMES

    theme = TuiConfig.model_fields["theme"].annotation
    assert tuple(THEME_NAMES) == get_args(theme)


async def test_menu_preview_names_the_theme(tmp_path):
    app, _ = _app_with_config(tmp_path, "tui:\n  theme: gigacode\n")
    async with app.run_test() as pilot:
        scr = _screen(app)
        app.push_screen(scr)
        await pilot.pause()
        rows = _texts(scr, ".set-menurow .set-rowtext")
        look = next(r for r in rows if "Внешний вид" in r)
        assert "Тема: GigaCode" in look
        assert "◂" not in look and "▸" not in look


async def test_row_shows_theme_name_and_explanation(tmp_path):
    app, _ = _app_with_config(tmp_path, "tui:\n  theme: auto\n")
    async with app.run_test() as pilot:
        scr = await _open_look(pilot)
        rows = _texts(scr, ".set-row")
        assert len(rows) == 1
        assert rows[0].startswith("▸ Тема")
        assert rows[0].rstrip().endswith("◂ Автоматическая ▸")
        assert "●" not in rows[0] and "○" not in rows[0]
        assert _texts(scr, ".set-hint") == [_AUTO_HINT]


async def test_arrows_cycle_names_and_write_config(tmp_path):
    app, cfg_file = _app_with_config(tmp_path, "tui:\n  theme: auto\n")
    async with app.run_test() as pilot:
        scr = await _open_look(pilot)
        await pilot.press("right")
        await pilot.pause(0.1)
        assert _theme_in_file(cfg_file) == "gigacode"
        assert _texts(scr, ".set-row")[0].rstrip().endswith("◂ GigaCode ▸")
        await pilot.press("right")
        await pilot.pause(0.1)
        assert _theme_in_file(cfg_file) == "auto"
        await pilot.press("left")
        await pilot.pause(0.1)
        assert _theme_in_file(cfg_file) == "gigacode"


async def test_gigacode_row_shows_its_own_hint(tmp_path):
    app, _ = _app_with_config(tmp_path, "tui:\n  theme: gigacode\n")
    async with app.run_test() as pilot:
        scr = await _open_look(pilot)
        assert _texts(scr, ".set-hint") == [_GIGACODE_HINT]


async def test_hint_follows_value_on_arrows(tmp_path):
    app, _ = _app_with_config(tmp_path, "tui:\n  theme: auto\n")
    async with app.run_test() as pilot:
        scr = await _open_look(pilot)
        await pilot.press("right")
        await pilot.pause(0.1)
        assert _texts(scr, ".set-hint") == [_GIGACODE_HINT]
        await pilot.press("left")
        await pilot.pause(0.1)
        assert _texts(scr, ".set-hint") == [_AUTO_HINT]


async def test_write_adds_missing_key_and_keeps_others(tmp_path):
    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "smart"\n'
    )
    async with app.run_test() as pilot:
        await _open_look(pilot)
        await pilot.press("right")
        await pilot.pause(0.1)
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert data["tui"]["theme"] == "gigacode"
        assert data["tui"]["finish_gate_mode"] == "smart"


async def test_write_is_success_that_applies_at_next_start(
    tmp_path, monkeypatch
):
    """Сохранено, действует со следующего запуска — это успех: зелёная ✓,
    путь файла — через `~`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    app, _ = _app_with_config(tmp_path, "tui:\n  theme: auto\n")
    async with app.run_test() as pilot:
        scr = await _open_look(pilot)
        await pilot.press("right")
        await pilot.pause(0.1)
        flash = scr.query_one("#set-flash", Static)
        assert flash.has_class("set-flash-ok")
        assert str(flash.render()) == (
            "✓ «Тема» — GigaCode, записано в ~/config.yml; "
            "применится со следующего запуска"
        )


async def test_footer_offers_only_working_keys(tmp_path):
    app, _ = _app_with_config(tmp_path, "tui:\n  theme: auto\n")
    async with app.run_test() as pilot:
        scr = await _open_look(pilot)
        footer = str(scr.query_one("#set-footer", Static).render())
        assert "←/→ — изменить" in footer
        assert "↑/↓" not in footer


async def test_theme_hint_height_stays_on_value_change(tmp_path):
    """Описание темы — с резервом под самое длинное: ←/→ меняет текст,
    высота подсказки (и низ рамки) на месте."""
    app, _ = _app_with_config(tmp_path, 'tui:\n  theme: "gigacode"\n')
    async with app.run_test(size=(70, 40)) as pilot:
        scr = await _open_look(pilot)

        def hint():
            return list(scr.query(".set-theme-hint"))[-1]

        heights = {hint().size.height}
        for _ in range(3):
            await pilot.press("right")
            await pilot.pause(0.1)
            heights.add(hint().size.height)
        assert heights == {2}
