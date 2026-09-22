import pytest

pytest.importorskip("textual")
from textual.widgets import Static


async def _open(pilot, scr, app, name):
    from gc_testkit.tui_support import open_settings_section
    app.push_screen(scr); await pilot.pause()
    open_settings_section(scr, name); await pilot.pause()


async def test_section_titles_show_section_name(tmp_path, monkeypatch):
    from gc_core import app_version
    from gc_core.updater import check as check_mod
    from gc_testkit.tui_support import open_settings_section
    from gc_tui import app as app_mod
    from gc_tui.screens.settings import SettingsScreen
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    monkeypatch.setattr(check_mod, "check_for_update",
                        lambda source, **k: check_mod.CheckResult("current", "old", "установлена последняя версия"))
    cfg = tmp_path / "c.yml"; cfg.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        await _open(pilot, scr, app, "Обновление")
        assert str(scr.query_one("#set-title", Static).render()) == "Обновление"
        open_settings_section(scr, "MCP-серверы"); await pilot.pause()
        assert str(scr.query_one("#set-title", Static).render()) == "MCP-серверы"


async def test_mcp_space_uninstalled_flashes_title_note(tmp_path, monkeypatch):
    from gc_core import app_version
    from gc_testkit.tui_support import open_settings_section
    from gc_tui import app as app_mod
    from gc_tui.screens.settings import SettingsScreen
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    cfg = tmp_path / "c.yml"; cfg.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        open_settings_section(scr, "MCP-серверы"); await pilot.pause()
        ed = scr._mcp_editor()
        # смоделировать «выбран неустановленный»: toggle вернёт подсказку
        monkeypatch.setattr(ed, "toggle", lambda: "«office» не установлен — Enter, чтобы установить")
        await pilot.press("space"); await pilot.pause()
        from textual.widgets import Static
        title = str(scr.query_one("#set-title", Static).render())
        assert "не установлен" in title                       # note в слоте заголовка
        assert not list(scr.query("#set-notice"))             # постоянного нижнего хинта нет
