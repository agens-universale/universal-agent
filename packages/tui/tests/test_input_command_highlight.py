"""Поле ввода: первое слово `/имя` цветом `SKILL`, если это известная
команда (или её алиас) или скилл по тому же точному разбору, что и
запуск (спека P3 §10, смоук P3a второй заход). Опечатка и обычный текст
не подсвечиваются; подсветка следует за набором."""

from __future__ import annotations

import pytest
from gc_testkit.tui_support import tui_settings
from gc_tui.palette import SKILL
from gc_tui.widgets.command_input import CommandTextArea
from rich.style import Style

_CACHE = [{"id": "supercoder:brainstorming"}]


def _colored(inp: CommandTextArea, row: int = 0) -> str:
    """Символы строки, окрашенные цветом `SKILL`."""
    line = inp.get_line(row)
    skill = Style.parse(SKILL).color
    return "".join(
        line.plain[s.start : s.end]
        for s in line.spans
        if Style.parse(str(s.style)).color == skill
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "typed, colored",
    [
        ("/brainstorming x", "/brainstorming"),
        ("/status", "/status"),
        ("/statuz", ""),
        ("просто текст /status", ""),
        ("/supercoder:brainstorming", "/supercoder:brainstorming"),
    ],
)
async def test_first_token_colored_only_when_known(tmp_path, typed, colored):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = list(_CACHE)
        inp = scr.query_one("#cmdinput", CommandTextArea)
        inp.focus()
        for ch in typed:
            await pilot.press(ch if ch != " " else "space")
        await pilot.pause()
        assert inp.text == typed
        assert _colored(inp) == colored


@pytest.mark.asyncio
async def test_highlight_follows_typing(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.screen.query_one("#cmdinput", CommandTextArea)
        inp.focus()
        for ch in "/statu":
            await pilot.press(ch)
        await pilot.pause()
        assert _colored(inp) == ""
        await pilot.press("s")
        await pilot.pause()
        assert _colored(inp) == "/status"
        await pilot.press("backspace")
        await pilot.pause()
        assert _colored(inp) == ""


@pytest.mark.asyncio
async def test_second_line_not_colored(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.screen.query_one("#cmdinput", CommandTextArea)
        inp.load_text("текст\n/status")
        await pilot.pause()
        assert _colored(inp, 1) == ""


def test_widget_without_predicate_is_plain():
    inp = CommandTextArea("/status x")
    assert _colored(inp) == ""


class _Reg:
    async def list_skills(self, *, pack=None, include_all=False):
        return list(_CACHE)


def _rendered_colored(inp: CommandTextArea) -> str:
    """Что реально нарисовано (`render_line`, с подложкой строки курсора и
    кэшем строк), окрашенное цветом `SKILL`."""
    skill = Style.parse(SKILL).color
    return "".join(
        seg.text
        for seg in inp.render_line(0)
        if seg.style and seg.style.color == skill
    )


@pytest.mark.asyncio
async def test_highlight_survives_real_render_and_warmup(
    tmp_path, monkeypatch, skills_warm
):
    """Живой путь: настоящий прогрев кэша, набор после старта, рисование
    через `render_line` (строка курсора не гасит цвет)."""
    pytest.importorskip("textual")
    from gc_core.tools.skills import registry as reg_mod
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp

    monkeypatch.setattr(
        reg_mod, "get_skills_registry", lambda cfg=None: _Reg()
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        inp = scr.query_one("#cmdinput", CommandTextArea)
        await wait_until(pilot, lambda: scr._skills_cache is not None, "кэш")
        inp.focus()
        for ch in "/brainstorming":
            await pilot.press(ch)
        await pilot.pause()
        assert _rendered_colored(inp) == "/brainstorming"


@pytest.mark.asyncio
async def test_highlight_appears_when_cache_warms_after_typing(tmp_path):
    """Набрали при холодном кэше; кэш пришёл позже — поле перерисовано."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        inp = scr.query_one("#cmdinput", CommandTextArea)
        inp.focus()
        for ch in "/brainstorming":
            await pilot.press(ch)
        await pilot.pause()
        assert _rendered_colored(inp) == ""
        scr._skills_cache = list(_CACHE)
        assert _rendered_colored(inp) == "/brainstorming"
        scr._skills_cache = None
        assert _rendered_colored(inp) == ""
