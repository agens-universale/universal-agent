import pytest
pytest.importorskip("textual")


async def test_update_action_rows_have_air(tmp_path, monkeypatch):
    import app_version, updater.check as check_mod, tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    from tests.tui.conftest import open_settings_section
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    monkeypatch.setattr(check_mod, "check_for_update",
                        lambda source, **k: check_mod.CheckResult("update", "new", "доступна новая версия"))
    cfg = tmp_path / "c.yml"; cfg.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        open_settings_section(scr, "Обновление")
        await pilot.pause(); await pilot.pause()
        rows = [w for w in scr.query(".set-row")
                if "set-spaced" in w.classes or "set-spaced-gap" in w.classes]
        assert len(rows) >= 3                       # 3 строки-действия с воздухом
        # фактический margin-bottom у всех строк-действий = 1 (равномерный воздух; регресс
        # Textual-грабли: раздельные .set-gap+.set-spaced на первой строке обнуляли mb)
        assert all(w.styles.margin.bottom == 1 for w in scr.query(".set-row"))
