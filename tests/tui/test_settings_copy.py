"""Ctrl+C на экране настроек: выделение копируется, пустое выделение — no-op
(приложение НЕ выходит в терминал)."""
import pytest
pytest.importorskip("textual")


async def test_ctrl_c_copies_selection_on_settings(tmp_path, monkeypatch):
    import app_version, tui.app as app_mod
    from tui.screens.settings import SettingsScreen
    monkeypatch.setattr(app_version, "install_source",
                        lambda _read=None: app_version.InstallSource("https://x/y", "master", "old", False))
    cfg = tmp_path / "c.yml"; cfg.write_text("backend: {}\n", encoding="utf-8")
    app = app_mod.TuiApp(config={})
    copied = {}
    async with app.run_test() as pilot:
        scr = SettingsScreen(config={}, config_path=str(cfg), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr); await pilot.pause()
        monkeypatch.setattr(app, "_current_selection", lambda: "http://x/v1")
        monkeypatch.setattr(app, "copy_to_clipboard", lambda s: copied.update(v=s))
        import tui.services.clipboard_ops as clipboard_ops
        monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda s: True)
        app.action_interrupt()                      # прямой вызов action — без реального ctrl+c
        assert copied.get("v") == "http://x/v1"     # выделение скопировано
        assert app.is_running                       # приложение не вышло


async def test_ctrl_c_no_selection_is_noop_on_settings(tmp_path, monkeypatch):
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
        monkeypatch.setattr(app, "_current_selection", lambda: "")
        app.action_interrupt()                      # пустое выделение → безвредно
        assert app.is_running                       # НЕ вышло
