"""TUI: сбор стартовых уведомлений; бейдж — в футере, в шапке только
готовность, MCP и хвост обновления."""

from __future__ import annotations

from gc_testkit.tui_support import as_settings, tui_settings
import pytest


def _header_text(scr) -> str:
    """Шапка так, как её раскладывает сам виджет по своей ширине."""
    header = scr.query_one("#brand-block")
    return header.render().laid_out(header.content_region.width).plain


@pytest.mark.asyncio
async def test_startup_block_collapsed_shows_max_three(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    # agent_md_path задан + agentsmd_override → notice agentsmd_override_ignored в TUI
    cfg = {
        "agent.model": "M",
        "backend.api_mode": "chat_completions",
        "agent.stop_tools": ["bogus1", "bogus2"],
        "tui.agentsmd_override": True,
        "paths.agent_md_path": "/some/agent.md",
        "backend.stream": True,  # устаревший ключ задан явно → deprecated_key
    }
    async with TuiApp(
        threads_dir=str(tmp_path / "t"), settings=tui_settings(cfg)
    ).run_test() as pilot:
        notices = (
            pilot.app._startup_notices()
        )  # владелец сборки — App, не экран
        # stop_tools_unknown + agentsmd_override_ignored + deprecated_key = 3
        assert len(notices) >= 3
        assert any(n.code == "stop_tools_unknown" for n in notices)


@pytest.mark.asyncio
async def test_notices_summary_in_footer_not_header_or_trace(tmp_path):
    """Бейдж уведомлений «⚠ N · F4/notices» — только в футере: в шапке его нет,
    в трейсе нет блока «Уведомления» — детали на экране /notices."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.notices_ui import WARN_GLYPH
    from textual.widgets import Static

    # stop_tools_unknown + agentsmd_override_ignored — два warning, без info.
    cfg = {
        "agent.model": "M",
        "agent.stop_tools": ["bogus1"],
        "tui.agentsmd_override": True,
        "paths.agent_md_path": "/some/agent.md",
    }
    async with TuiApp(
        threads_dir=str(tmp_path / "t"), settings=tui_settings(cfg)
    ).run_test(size=(120, 40)) as pilot:
        scr = pilot.app.screen
        await pilot.pause()
        header = _header_text(scr)
        assert "F4/notices" not in header and WARN_GLYPH not in header
        assert header.splitlines()[-1] == "✓ готов к работе"
        footer = str(scr.query_one("#footer-slot").render())
        assert f"{WARN_GLYPH} 2 · F4/notices" in footer
        texts = [
            str(s.render())
            for s in pilot.app.query_one("#trace").query(Static)
        ]
        assert not any("Уведомления ·" in t for t in texts), (
            "Блок уведомлений в трейсе не должен монтироваться"
        )


@pytest.mark.asyncio
async def test_update_available_tail_in_header(tmp_path):
    """Найденное обновление (async авто-чек) — хвост строки версии в шапке;
    строка статуса не меняется."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    cfg = {"agent.model": "M"}
    async with TuiApp(
        threads_dir=str(tmp_path / "t"), settings=tui_settings(cfg)
    ).run_test(size=(120, 40)) as pilot:
        scr = pilot.app.screen
        await pilot.pause()
        assert "доступна новая версия" not in _header_text(scr)  # до чека
        pilot.app._update_available = True
        scr.refresh_update_notice()
        await pilot.pause()
        lines = _header_text(scr).splitlines()
        assert "↑ доступна новая версия · /settings → Обновление" in lines[1]
        assert lines[-1] == "✓ готов к работе"


@pytest.mark.asyncio
async def test_notices_screen_includes_update_available(tmp_path):
    """/notices (F4) содержит запись об обновлении, когда авто-чек нашёл версию.
    M1: текст уведомления — БЕЗ ведущего ↑ (NoticesScreen._notice_text сам подставляет
    глиф уровня — иначе рендерится «ℹ ↑ доступна…», двойной глиф)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.notices import NoticesScreen, _notice_text

    cfg = {"agent.model": "M"}
    async with TuiApp(
        threads_dir=str(tmp_path / "t"), settings=tui_settings(cfg)
    ).run_test() as pilot:
        await pilot.pause()
        pilot.app._update_available = True
        pilot.app.action_notices()
        await pilot.pause()
        assert isinstance(pilot.app.screen, NoticesScreen)
        # _load_notices — тот же путь, которым экран берёт список в on_mount (app.collect_notices)
        notice = next(
            n
            for n in pilot.app.screen._load_notices()
            if n.code == "update_available"
        )
        assert not notice.message.startswith("↑")
        rendered = str(_notice_text(notice))
        assert "↑" not in rendered  # уровневый глиф (ℹ) — единственный


@pytest.mark.asyncio
async def test_auto_check_updates_main_header_when_other_screen_on_top(
    tmp_path, monkeypatch
):
    """M3: авто-чек на старте маршаллит результат в ГЛАВНЫЙ экран (app._main_screen()),
    а не в активный (app.screen) — иначе, если сверху стека другой экран (напр.
    SettingsScreen) в момент, когда воркер прилетает, карточка так и не обновится за
    всю сессию."""
    pytest.importorskip("textual")
    import time as time_mod

    from gc_core.updater import check as check_mod
    from gc_core.updater import source as source_mod
    from gc_core.updater.check import CheckResult
    from gc_core.updater.source import UpdateSource
    from gc_tui.app import TuiApp
    from gc_tui.screens.settings import SettingsScreen

    def _slow_resolved(*a, **k):
        return UpdateSource(url="https://x/y", ref="master", commit="old")

    def _slow_check(src, **k):
        time_mod.sleep(
            0.15
        )  # даёт время протолкнуть SettingsScreen поверх стека
        return CheckResult("update", "new", "доступна новая версия")

    monkeypatch.setattr(source_mod, "resolved_source", _slow_resolved)
    monkeypatch.setattr(check_mod, "check_for_update", _slow_check)
    cfg_file = tmp_path / "c.yml"
    cfg_file.write_text("backend: {}\n", encoding="utf-8")
    cfg = {"agent.model": "M"}
    app = TuiApp(threads_dir=str(tmp_path / "t"), settings=tui_settings(cfg))
    async with app.run_test() as pilot:
        scr = SettingsScreen(
            settings=as_settings(cfg),
            config_path=str(cfg_file),
            on_apply=lambda: None,
            probe_fn=lambda *a, **k: None,
        )
        app.push_screen(
            scr
        )  # НЕ главный экран — на нём нет refresh_update_notice
        await pilot.pause()
        for _ in range(60):
            await pilot.pause(0.05)
            if app._update_available:
                break
        assert app._update_available is True
        assert "↑ доступна новая версия" in _header_text(app._main_screen())


@pytest.mark.asyncio
async def test_warning_toasts_fire_on_change_then_silent(
    tmp_path, monkeypatch
):
    """Warning-уведомления всплывают тостом на 1-м старте (severity=warning), а на 2-м
    с ТЕМ ЖЕ набором — молчат (on-change; постоянная видимость держит карточка/notices)."""
    pytest.importorskip("textual")
    from gc_core import upgrade_gate
    from gc_tui.app import TuiApp

    state_path = tmp_path / "agent_state.json"
    monkeypatch.setattr(upgrade_gate, "_state_path", lambda: state_path)
    cfg = {
        "agent.model": "M",
        "agent.stop_tools": ["bogus1"],
        "backend.stream": True,  # устаревший ключ задан явно → deprecated_key
    }

    t1 = []
    app1 = TuiApp(threads_dir=str(tmp_path / "t"), settings=tui_settings(cfg))
    monkeypatch.setattr(
        app1, "notify", lambda msg, **k: t1.append(k.get("severity"))
    )
    async with app1.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
    assert "warning" in t1  # 1-й старт: warning-тосты всплыли
    assert upgrade_gate.read_state().get(
        "last_shown_warning_codes"
    )  # набор сохранён

    t2 = []
    app2 = TuiApp(threads_dir=str(tmp_path / "t"), settings=tui_settings(cfg))
    monkeypatch.setattr(
        app2, "notify", lambda msg, **k: t2.append(k.get("severity"))
    )
    async with app2.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
    assert "warning" not in t2  # 2-й старт, тот же набор — тишина


@pytest.mark.asyncio
async def test_header_readiness_always_plain(tmp_path):
    """Строка готовности в шапке всегда простая «✓ готов к работе»: счётчики
    уведомлений живут в футере, не на готовности."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    cfg = {
        "agent.model": "M",
        "agent.stop_tools": ["bogus1"],
        "backend.stream": True,  # устаревший ключ задан явно → deprecated_key
    }
    async with TuiApp(
        threads_dir=str(tmp_path / "t"), settings=tui_settings(cfg)
    ).run_test(size=(120, 40)) as pilot:
        scr = pilot.app.screen
        await pilot.pause()
        assert _header_text(scr).splitlines()[-1] == "✓ готов к работе"
