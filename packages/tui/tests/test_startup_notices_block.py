"""TUI: сбор стартовых уведомлений, бейдж готовности, сворачиваемый блок."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_startup_block_collapsed_shows_max_three(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    # agent_md_path задан + agentsmd_override → notice agentsmd_override_ignored в TUI
    cfg = {"model": "M", "backend_api_mode": "chat_completions",
           "stop_tools": ["bogus1", "bogus2"], "agentsmd_override": True,
           "agent_md_path": "/some/agent.md",
           "deprecated_keys_used": ["backend.stream"]}
    async with TuiApp(threads_dir=str(tmp_path / "t"), config=cfg).run_test() as pilot:
        notices = pilot.app._startup_notices()      # владелец сборки — App, не экран
        # stop_tools_unknown + agentsmd_override_ignored + deprecated_key = 3
        assert len(notices) >= 3
        assert any(n.code == "stop_tools_unknown" for n in notices)


@pytest.mark.asyncio
async def test_notices_summary_in_card_not_trace(tmp_path):
    """При уведомлениях в КАРТОЧКЕ строка «⚠ N · F4/notices» (формат B); в трейсе
    НЕТ блока-заголовка «Уведомления» — детали только на экране /notices."""
    pytest.importorskip("textual")
    from textual.widgets import Static
    from gc_tui.app import TuiApp
    cfg = {"model": "M", "stop_tools": ["bogus1"],
           "deprecated_keys_used": ["backend.stream"]}
    async with TuiApp(threads_dir=str(tmp_path / "t"), config=cfg).run_test() as pilot:
        scr = pilot.app.screen
        await pilot.pause()
        card = str(scr._mcp_status_row.render())
        assert "⚠" in card and "F4/notices" in card, (
            f"Строка уведомлений не найдена в карточке: {card!r}"
        )
        # Блок-заголовок «Уведомления · N» в трейсе не монтируется.
        texts = [str(s.render()) for s in pilot.app.query_one("#trace").query(Static)]
        assert not any("Уведомления ·" in t for t in texts), (
            "Блок уведомлений в трейсе не должен монтироваться"
        )


@pytest.mark.asyncio
async def test_notices_line_format_b_glyph_and_breakdown(tmp_path):
    """Формат B: ведущий severity-глиф, разбивка по уровням, без слова «уведомления»."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    # stop_tools_unknown + agentsmd_override_ignored — оба warning, без info: чистый warn-only случай.
    cfg = {"model": "M", "stop_tools": ["bogus1"], "agentsmd_override": True,
           "agent_md_path": "/some/agent.md"}
    async with TuiApp(threads_dir=str(tmp_path / "t"), config=cfg).run_test() as pilot:
        scr = pilot.app.screen
        await pilot.pause()
        card = str(scr._mcp_status_row.render())
        from gc_tui.services.notices_ui import WARN_GLYPH
        assert "уведомления:" not in card                       # слово убрано
        # Значок из общего источника: в нём вариационный селектор, и литерал разошёлся
        # бы с кодом по невидимому символу. Число склеено со значком — иначе перенос
        # строки в футере разрывал пару.
        assert f"{WARN_GLYPH} 2 · F4/notices" in card            # 2 warning'а, формат B
        # строка уведомлений — базовым цветом карточки, без амбер-подсветки глифа
        from gc_tui.palette import WARN
        t = scr._compose_card_lines(None)
        assert not any(sp.style == WARN for sp in t.spans)


def test_compose_card_lines_return_annotation_matches_actual_type():
    """M2: _compose_card_lines собирает и возвращает rich.text.Text (не str) —
    аннотация врала `-> str`."""
    from gc_tui.screens.main import TraceScreen
    assert TraceScreen._compose_card_lines.__annotations__["return"] == "Text"


@pytest.mark.asyncio
async def test_no_notices_line_when_clean_config(tmp_path):
    """Без уведомлений — строки «уведомления:» в карточке нет, готовность простая."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    cfg = {"model": "M"}
    async with TuiApp(threads_dir=str(tmp_path / "t"), config=cfg).run_test() as pilot:
        scr = pilot.app.screen
        await pilot.pause()
        card = str(scr._mcp_status_row.render())
        assert "уведомления:" not in card
        assert "готов к работе" in card


@pytest.mark.asyncio
async def test_update_available_line_in_card_teal(tmp_path):
    """При найденном обновлении (async авто-чек) в карточке — постоянная строка
    «↑ доступна новая версия · Настройки → Обновление» accent-цветом; refresh дорисовывает."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.palette import ACCENT
    cfg = {"model": "M"}
    async with TuiApp(threads_dir=str(tmp_path / "t"), config=cfg).run_test() as pilot:
        scr = pilot.app.screen
        await pilot.pause()
        assert "доступна новая версия" not in str(scr._mcp_status_row.render())   # до чека — нет
        pilot.app._update_available = True
        scr.refresh_update_notice()
        card = str(scr._mcp_status_row.render())
        assert "↑ доступна новая версия" in card and "/settings" in card
        assert "готов к работе" in card
        t = scr._compose_card_lines(None)
        assert any(sp.style == ACCENT for sp in t.spans)   # строка обновления — accent


@pytest.mark.asyncio
async def test_notices_screen_includes_update_available(tmp_path):
    """/notices (F4) содержит запись об обновлении, когда авто-чек нашёл версию.
    M1: текст уведомления — БЕЗ ведущего ↑ (NoticesScreen._notice_text сам подставляет
    глиф уровня — иначе рендерится «ℹ ↑ доступна…», двойной глиф)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.notices import NoticesScreen, _notice_text
    cfg = {"model": "M"}
    async with TuiApp(threads_dir=str(tmp_path / "t"), config=cfg).run_test() as pilot:
        await pilot.pause()
        pilot.app._update_available = True
        pilot.app.action_notices()
        await pilot.pause()
        assert isinstance(pilot.app.screen, NoticesScreen)
        # _load_notices — тот же путь, которым экран берёт список в on_mount (app.collect_notices)
        notice = next(n for n in pilot.app.screen._load_notices() if n.code == "update_available")
        assert not notice.message.startswith("↑")
        rendered = str(_notice_text(notice))
        assert "↑" not in rendered                          # уровневый глиф (ℹ) — единственный


@pytest.mark.asyncio
async def test_auto_check_updates_main_card_when_other_screen_on_top(tmp_path, monkeypatch):
    """M3: авто-чек на старте маршаллит результат в ГЛАВНЫЙ экран (app._main_screen()),
    а не в активный (app.screen) — иначе, если сверху стека другой экран (напр.
    SettingsScreen) в момент, когда воркер прилетает, карточка так и не обновится за
    всю сессию."""
    pytest.importorskip("textual")
    import time as time_mod
    from gc_core.updater import source as source_mod
    from gc_core.updater import check as check_mod
    from gc_core.updater.check import CheckResult
    from gc_core.updater.source import UpdateSource
    from gc_tui.app import TuiApp
    from gc_tui.screens.settings import SettingsScreen

    def _slow_resolved(*a, **k):
        return UpdateSource(url="https://x/y", ref="master", commit="old")

    def _slow_check(src, **k):
        time_mod.sleep(0.15)          # даёт время протолкнуть SettingsScreen поверх стека
        return CheckResult("update", "new", "доступна новая версия")

    monkeypatch.setattr(source_mod, "resolved_source", _slow_resolved)
    monkeypatch.setattr(check_mod, "check_for_update", _slow_check)
    cfg_file = tmp_path / "c.yml"; cfg_file.write_text("backend: {}\n", encoding="utf-8")
    cfg = {"model": "M"}
    app = TuiApp(threads_dir=str(tmp_path / "t"), config=cfg)
    async with app.run_test() as pilot:
        scr = SettingsScreen(config=cfg, config_path=str(cfg_file), on_apply=lambda: None,
                             probe_fn=lambda *a, **k: None)
        app.push_screen(scr)                 # НЕ главный экран — на нём нет refresh_update_notice
        await pilot.pause()
        for _ in range(60):
            await pilot.pause(0.05)
            if app._update_available:
                break
        assert app._update_available is True
        main = app._main_screen()
        card = str(main._mcp_status_row.render())
        assert "↑ доступна новая версия" in card


@pytest.mark.asyncio
async def test_warning_toasts_fire_on_change_then_silent(tmp_path, monkeypatch):
    """Warning-уведомления всплывают тостом на 1-м старте (severity=warning), а на 2-м
    с ТЕМ ЖЕ набором — молчат (on-change; постоянная видимость держит карточка/notices)."""
    pytest.importorskip("textual")
    from gc_core import upgrade_gate
    from gc_tui.app import TuiApp
    state_path = tmp_path / "agent_state.json"
    monkeypatch.setattr(upgrade_gate, "_state_path", lambda: state_path)
    cfg = {"model": "M", "stop_tools": ["bogus1"], "deprecated_keys_used": ["backend.stream"]}

    t1 = []
    app1 = TuiApp(threads_dir=str(tmp_path / "t"), config=cfg)
    monkeypatch.setattr(app1, "notify", lambda msg, **k: t1.append(k.get("severity")))
    async with app1.run_test() as pilot:
        await pilot.pause(); await pilot.pause()
    assert "warning" in t1                                   # 1-й старт: warning-тосты всплыли
    assert upgrade_gate.read_state().get("last_shown_warning_codes")   # набор сохранён

    t2 = []
    app2 = TuiApp(threads_dir=str(tmp_path / "t"), config=cfg)
    monkeypatch.setattr(app2, "notify", lambda msg, **k: t2.append(k.get("severity")))
    async with app2.run_test() as pilot:
        await pilot.pause(); await pilot.pause()
    assert "warning" not in t2                               # 2-й старт, тот же набор — тишина


@pytest.mark.asyncio
async def test_card_readiness_always_plain(tmp_path):
    """Строка готовности в карточке всегда простая «✓ готов к работе» (счётчики —
    на отдельной строке «уведомления:», не на готовности)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    cfg = {"model": "M", "stop_tools": ["bogus1"],
           "deprecated_keys_used": ["backend.stream"]}
    async with TuiApp(threads_dir=str(tmp_path / "t"), config=cfg).run_test() as pilot:
        scr = pilot.app.screen
        await pilot.pause()
        card = str(scr._mcp_status_row.render())
        assert "✓ готов к работе" in card
