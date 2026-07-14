import pytest
pytest.importorskip("textual")
from textual.widgets import Static


async def _open(pilot, scr, app, i):
    app.push_screen(scr); await pilot.pause()
    scr._menu_idx = i; scr._open_section(i); await pilot.pause()


async def test_section_titles_show_section_name(tmp_path, monkeypatch):
    import app_version, updater.check as check_mod, tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    monkeypatch.setattr(check_mod, "check_for_update",
                        lambda source, **k: check_mod.CheckResult("current", "old", "установлена последняя версия"))
    cfg = tmp_path / "c.yml"; cfg.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        await _open(pilot, scr, app, 4)                       # Обновление
        assert str(scr.query_one("#set-title", Static).render()) == "Обновление"
        scr._open_section(3); await pilot.pause()             # MCP
        assert str(scr.query_one("#set-title", Static).render()) == "MCP-серверы"


async def test_mcp_space_uninstalled_flashes_title_note(tmp_path, monkeypatch):
    import app_version, tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    cfg = tmp_path / "c.yml"; cfg.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        scr._menu_idx = 3; scr._open_section(3); await pilot.pause()
        ed = scr._mcp_editor()
        # смоделировать «выбран неустановленный»: toggle вернёт подсказку
        monkeypatch.setattr(ed, "toggle", lambda: "«office» не установлен — Enter, чтобы установить")
        await pilot.press("space"); await pilot.pause()
        from textual.widgets import Static
        title = str(scr.query_one("#set-title", Static).render())
        assert "не установлен" in title                       # note в слоте заголовка
        assert not list(scr.query("#set-notice"))             # постоянного нижнего хинта нет
