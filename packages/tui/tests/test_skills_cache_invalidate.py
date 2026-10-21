import logging

from gc_testkit.tui_support import tui_settings
import pytest


class _Reg:
    def __init__(self, entries=None, exc=None):
        self.entries = entries or []
        self.exc = exc
        self.calls = 0

    async def list_skills(self, *, pack=None, include_all=False):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return list(self.entries)


def _patch_registry(monkeypatch, reg):
    from gc_core.tools.skills import registry as reg_mod

    monkeypatch.setattr(reg_mod, "get_skills_registry", lambda cfg=None: reg)


@pytest.mark.asyncio
async def test_skills_cache_invalidated_on_run_end(tmp_path, monkeypatch):
    """Спадающий фронт сбрасывает старый снимок и тихо прогревает свежий."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _patch_registry(monkeypatch, _Reg([{"id": "fresh"}]))

    async with TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "old"}]  # «старый» снимок
        scr._was_running = True  # был прогон
        scr.app.active_runner = None  # runner завершился → is_running False
        scr._poll_active()
        await pilot.pause()
        # сброшен на спадающем фронте и прогрет заново
        assert scr._skills_cache == [{"id": "fresh"}]


@pytest.mark.asyncio
async def test_skills_cache_kept_while_idle(tmp_path):
    """Без спадающего фронта (не было прогона) кэш не сбрасывается."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "old"}]
        scr._was_running = False  # прогона не было
        scr.app.active_runner = None
        scr._poll_active()
        await pilot.pause()
        assert scr._skills_cache == [{"id": "old"}]  # не тронут


@pytest.mark.asyncio
async def test_skills_cache_warmed_on_mount(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _patch_registry(monkeypatch, _Reg([{"id": "p:brainstorming"}]))
    async with TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    ).run_test() as pilot:
        await pilot.pause()
        assert pilot.app.screen._skills_cache == [{"id": "p:brainstorming"}]


@pytest.mark.asyncio
async def test_skills_warm_error_goes_to_log_not_trace(
    tmp_path, monkeypatch, caplog
):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _patch_registry(monkeypatch, _Reg(exc=RuntimeError("boom")))
    caplog.set_level(logging.WARNING, logger="gc_tui.screens.main")
    async with TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    ).run_test() as pilot:
        await pilot.pause()
        scr = pilot.app.screen
        assert scr._skills_cache is None  # холодный — /skill загрузит сам
        trace_text = "\n".join(
            str(w.render()) for w in scr.query("#trace Static")
        )
        assert "boom" not in trace_text
        assert "/skills" not in trace_text
        assert "boom" in caplog.text
