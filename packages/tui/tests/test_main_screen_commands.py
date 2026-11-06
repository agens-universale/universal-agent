"""Парсинг /команд + inline-ошибка с closest-match (§4)."""

import pytest


@pytest.mark.asyncio
async def test_unknown_command_shows_closest_match():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/statuz"
        await pilot.press("enter")
        # Должна появиться inline-ошибка [!] с closest
        trace = pilot.app.query_one("#trace")
        text = "\n".join(str(s.render()) for s in trace.query("Static"))
        assert "[!]" in text
        assert "/statuz" in text
        assert "вы имели в виду" in text


@pytest.mark.asyncio
async def test_notices_command_opens_screen():
    # Набранная /notices должна открыть NoticesScreen (диспатч-ветка, не только реестр).
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.notices import NoticesScreen

    async with TuiApp().run_test() as pilot:
        await pilot.app.screen._handle_command("notices")
        await pilot.pause()
        assert isinstance(pilot.app.screen, NoticesScreen)


@pytest.mark.asyncio
async def test_unknown_command_no_close_match():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/zzzzzz9999"
        await pilot.press("enter")
        trace = pilot.app.query_one("#trace")
        text = "\n".join(str(s.render()) for s in trace.query("Static"))
        assert "[!]" in text
        assert "вы имели в виду" not in text


_SKILLS = [
    {"id": "supercoder:brainstorming", "pack_id": "supercoder"},
    {"id": "p:status"},
    {"id": "a:review"},
    {"id": "b:review"},
]


def _trace_text(pilot) -> str:
    trace = pilot.app.query_one("#trace")
    return "\n".join(str(s.render()) for s in trace.query("Static"))


async def _submit(pilot, text: str) -> None:
    pilot.app.screen._skills_cache = list(_SKILLS)
    inp = pilot.app.query_one("#cmdinput")
    inp.text = text
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause(0.1)


@pytest.mark.asyncio
async def test_command_wins_over_same_named_skill():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        pilot.app.screen._skills_cache = list(_SKILLS)
        await pilot.app.screen._handle_command("status")
        await pilot.pause(0.1)
        assert pilot.app.tui_state.pending_skill_id is None
        assert "неизвестная команда" not in _trace_text(pilot)


@pytest.mark.asyncio
async def test_command_typo_suggests_command_and_runs_no_skill():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        await _submit(pilot, "/statuz")
        text = _trace_text(pilot)
        assert "неизвестная команда: /statuz" in text
        assert "вы имели в виду /status" in text
        assert pilot.app.tui_state.pending_skill_id is None


@pytest.mark.asyncio
async def test_skill_typo_suggests_skill_and_runs_nothing():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        await _submit(pilot, "/brainstormin идея")
        text = _trace_text(pilot)
        assert "неизвестная команда: /brainstormin" in text
        assert "/brainstorming" in text
        assert pilot.app.tui_state.pending_skill_id is None
        assert not _running(pilot.app)


@pytest.mark.asyncio
async def test_ambiguous_short_skill_name_lists_full_ids():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        await _submit(pilot, "/review код")
        text = _trace_text(pilot)
        assert "/a:review" in text and "/b:review" in text
        assert pilot.app.tui_state.pending_skill_id is None
        assert not _running(pilot.app)


@pytest.mark.asyncio
async def test_slash_skill_name_without_text_pends_skill():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        pilot.app.screen._skills_cache = list(_SKILLS)
        await pilot.app.screen._handle_command("brainstorming")
        await pilot.pause(0.1)
        assert (
            pilot.app.tui_state.pending_skill_id == "supercoder:brainstorming"
        )


@pytest.mark.asyncio
async def test_ask_human_slash_skill_is_command_only_with_warm_cache():
    """Вопрос агента: `/имя` скилла — команда при прогретом кэше; холодный
    кэш (None) — ответ агенту, как раньше."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.state import AskHumanState

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.tui_state.ask_human_state = AskHumanState(
            call_id="c1", question="Q?"
        )
        assert scr._skills_cache is None
        assert not scr._is_command_input("/brainstorming x")
        scr._skills_cache = list(_SKILLS)
        assert scr._is_command_input("/brainstorming x")
        assert not scr._is_command_input("/brainstormin x")
        assert not scr._is_command_input("/review x")
        assert scr._is_command_input("/status")


def _running(app) -> bool:
    runner = app.active_runner
    return runner is not None and runner.is_running()


@pytest.mark.asyncio
async def test_typo_on_ambiguous_short_name_suggests_full_ids_only():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        pilot.app.screen._skills_cache = list(_SKILLS)
        cands = pilot.app.screen._command_candidates("revie")
        assert "review" not in cands
        assert cands[-2:] == ["a:review", "b:review"]


@pytest.mark.asyncio
async def test_typo_suggests_skill_shadowed_by_command_as_full_id():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        pilot.app.screen._skills_cache = list(_SKILLS)
        cands = pilot.app.screen._command_candidates("statsu")
        assert cands[0] == "status"
        assert "p:status" in cands
        assert cands.count("status") == 1


@pytest.mark.asyncio
async def test_skill_command_with_pack_name_pends_bootstrap():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        pilot.app.screen._skills_cache = [
            {
                "id": "codebase:entry",
                "pack_id": "codebase",
                "is_pack_bootstrap": True,
            },
            {"id": "codebase:other", "pack_id": "codebase"},
        ]
        await pilot.app.screen._handle_command("skill codebase")
        await pilot.pause(0.1)
        assert pilot.app.tui_state.pending_skill_id == "codebase:entry"


@pytest.mark.asyncio
async def test_ask_human_path_like_input_is_not_command():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.state import AskHumanState

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.tui_state.ask_human_state = AskHumanState(
            call_id="c1", question="Q?"
        )
        scr._skills_cache = list(_SKILLS)
        assert not scr._is_command_input("/path/to/file")


@pytest.mark.asyncio
async def test_cold_cache_slash_skill_name_is_unknown_command():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = None
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/brainstorming x"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert "неизвестная команда: /brainstorming" in _trace_text(pilot)
        assert pilot.app.tui_state.pending_skill_id is None
        assert not _running(pilot.app)
