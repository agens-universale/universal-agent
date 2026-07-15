"""Хаб настроек: глиф «Обновление» зеркалит СТАРТОВЫЙ авто-чек, не требуя захода в секцию.

Стартовый чек идёт при каждом запуске (update.check: auto) — его вердикт живёт на app
(`_update_check_result` / `_update_checking`) и является источником для строки меню:
`↑` доступно · `✓` актуально · `—` нет вердикта · `…` проверка в полёте. Секция, открытая
позже, подхватывает тот же вердикт и сеть повторно не дёргает.
"""
import pytest

pytest.importorskip("textual")

from updater.check import CheckResult


def _screen(tmp_path):
    from tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text("backend: {}\n", encoding="utf-8")
    return SettingsScreen(config={}, config_path=str(cfg_file), on_apply=lambda: None,
                          probe_fn=lambda *a, **k: None)


@pytest.mark.asyncio
async def test_hub_glyph_current_from_startup_check(tmp_path):
    """Стартовый чек сказал «актуально» → в хабе сразу ✓, без захода в секцию."""
    import tui.app as app_mod
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        app._update_check_result = CheckResult("current", "abc", "установлена последняя версия")
        app._update_checking = False
        scr = _screen(tmp_path)
        app.push_screen(scr)
        await pilot.pause()
        assert scr._menu_glyph(4) == ("✓", __import__("tui.palette", fromlist=["OK"]).OK)


@pytest.mark.asyncio
async def test_hub_glyph_wave_while_startup_check_in_flight(tmp_path):
    """Пока стартовый чек в полёте — «…», а не «—» (вердикта ещё нет, но он будет)."""
    import tui.app as app_mod
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        app._update_check_result = None
        app._update_checking = True
        scr = _screen(tmp_path)
        app.push_screen(scr)
        await pilot.pause()
        glyph, _ = scr._menu_glyph(4)
        assert glyph == "…"


@pytest.mark.asyncio
async def test_section_reuses_startup_verdict_without_network(tmp_path, monkeypatch):
    """Вход в секцию при готовом стартовом вердикте не делает второй сетевой запрос."""
    import tui.app as app_mod
    import updater.check as check_mod
    calls = []
    monkeypatch.setattr(check_mod, "check_for_update",
                        lambda source, **k: calls.append(source) or CheckResult("current", "abc", "ок"))
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        app._update_check_result = CheckResult("current", "abc", "установлена последняя версия")
        scr = _screen(tmp_path)
        app.push_screen(scr)
        await pilot.pause()
        scr._menu_idx = 4
        scr._open_section(4)
        await pilot.pause(); await pilot.pause()
        assert calls == []                                   # сеть не трогали — вердикт уже есть
        assert "установлена последняя версия" in str(scr._checked_line_text())


@pytest.mark.asyncio
async def test_manual_check_publishes_verdict_to_app(tmp_path):
    """Ручная проверка в секции кладёт вердикт на app — общий источник для хаба и карточки."""
    import tui.app as app_mod
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = _screen(tmp_path)
        app.push_screen(scr)
        await pilot.pause()
        scr._apply_check_result(CheckResult("update", "new", "доступна новая версия"))
        await pilot.pause()
        assert app._update_check_result.status == "update"
