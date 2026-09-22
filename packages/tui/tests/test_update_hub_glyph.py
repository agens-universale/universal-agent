"""Хаб настроек: глиф «Обновление» зеркалит СТАРТОВЫЙ авто-чек, не требуя захода в секцию.

Стартовый чек идёт при каждом запуске (update.check: auto) — его вердикт живёт на app
(`_update_check_result` / `_update_checking`) и является источником для строки меню:
`↑` доступно · `✓` актуально · `—` нет вердикта · `…` проверка в полёте. Секция, открытая
позже, подхватывает тот же вердикт и сеть повторно не дёргает.
"""
import pytest

pytest.importorskip("textual")

from gc_core.updater.check import CheckResult
from gc_testkit.tui_support import open_settings_section, settings_section_index


def _screen(tmp_path):
    from gc_tui.screens.settings import SettingsScreen
    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text("backend: {}\n", encoding="utf-8")
    return SettingsScreen(config={}, config_path=str(cfg_file), on_apply=lambda: None,
                          probe_fn=lambda *a, **k: None)


@pytest.mark.asyncio
async def test_hub_glyph_current_from_startup_check(tmp_path):
    """Стартовый чек сказал «актуально» → в хабе сразу ✓, без захода в секцию."""
    from gc_tui import app as app_mod
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        app._update_check_result = CheckResult("current", "abc", "установлена последняя версия")
        app._update_checking = False
        scr = _screen(tmp_path)
        app.push_screen(scr)
        await pilot.pause()
        assert scr._menu_glyph(settings_section_index("Обновление")) == (
            "✓", __import__("gc_tui.palette", fromlist=["OK"]).OK)


@pytest.mark.asyncio
async def test_hub_glyph_wave_while_startup_check_in_flight(tmp_path):
    """Пока стартовый чек в полёте — «…», а не «—» (вердикта ещё нет, но он будет)."""
    from gc_tui import app as app_mod
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        app._update_check_result = None
        app._update_checking = True
        scr = _screen(tmp_path)
        app.push_screen(scr)
        await pilot.pause()
        glyph, _ = scr._menu_glyph(settings_section_index("Обновление"))
        assert glyph == "…"


@pytest.mark.asyncio
async def test_section_reuses_startup_verdict_without_network(tmp_path, monkeypatch):
    """Вход в секцию при готовом стартовом вердикте не делает второй сетевой запрос."""
    from gc_core.updater import check as check_mod
    from gc_tui import app as app_mod
    calls = []
    monkeypatch.setattr(check_mod, "check_for_update",
                        lambda source, **k: calls.append(source) or CheckResult("current", "abc", "ок"))
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        app._update_check_result = CheckResult("current", "abc", "установлена последняя версия")
        scr = _screen(tmp_path)
        app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Обновление")
        await pilot.pause(); await pilot.pause()
        assert calls == []                                   # сеть не трогали — вердикт уже есть
        assert "установлена последняя версия" in str(scr._checked_line_text())


@pytest.mark.asyncio
async def test_manual_check_publishes_verdict_to_app(tmp_path):
    """Ручная проверка в секции кладёт вердикт на app — общий источник для хаба и карточки."""
    from gc_tui import app as app_mod
    app = app_mod.TuiApp(config={})
    async with app.run_test() as pilot:
        scr = _screen(tmp_path)
        app.push_screen(scr)
        await pilot.pause()
        scr._apply_check_result(CheckResult("update", "new", "доступна новая версия"))
        await pilot.pause()
        assert app._update_check_result.status == "update"
