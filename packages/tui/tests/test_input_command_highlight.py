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
        ("/сжать", "/сжать"),
        ("/?", "/?"),
        ("/ыефегы", "/ыефегы"),
        ("/икфштыещкьштп", "/икфштыещкьштп"),
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


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        settings=tui_settings({"agent.model": "M"}),
    )


async def _type(pilot, inp, typed: str) -> None:
    for ch in typed:
        await pilot.press(ch if ch != " " else "space")
    await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "typed, shown",
    [
        ("/туц ", "/new "),
        ("/сдуфк ", "/clear "),
        ("/очистить ", "/new "),
        ("/навык ", "/skill "),
        ("/сжать ", "/compact "),
        ("/икфштыещкьштп ", "/brainstorming "),
        ("/clear ", "/clear "),
        ("/new ", "/new "),
        ("/brainstorming ", "/brainstorming "),
        ("/туцы ", "/туцы "),
    ],
)
async def test_space_writes_command_in_latin(tmp_path, typed, shown):
    """В поле команда пишется латиницей: пробел переводит слово в чужой
    раскладке, русское слово заменяет его командой. Английский синоним,
    имя и незнакомое слово остаются как набраны."""
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = list(_CACHE)
        inp = scr.query_one("#cmdinput", CommandTextArea)
        inp.focus()
        await _type(pilot, inp, typed)
        assert inp.text == shown
        assert inp.cursor_location == (0, len(shown))


@pytest.mark.asyncio
async def test_space_fix_reports_one_change(tmp_path, monkeypatch):
    """Пробел и перевод — два шага отмены, но экран видит одно изменение
    поля, сразу с итоговым текстом."""
    pytest.importorskip("textual")
    from gc_tui.screens.main import TraceScreen

    seen: list[str] = []
    handler = TraceScreen.on_text_area_changed

    async def spy(self, event):
        seen.append(event.text_area.text)
        await handler(self, event)

    async with _app(tmp_path).run_test() as pilot:
        inp = pilot.app.screen.query_one("#cmdinput", CommandTextArea)
        inp.focus()
        await _type(pilot, inp, "/туц")
        monkeypatch.setattr(TraceScreen, "on_text_area_changed", spy)
        await _type(pilot, inp, " ")
        assert seen == ["/new "]


@pytest.mark.asyncio
async def test_inserted_text_is_not_rewritten(tmp_path):
    """Только нажатый пробел: вставка и история остаются как есть."""
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        inp = pilot.app.screen.query_one("#cmdinput", CommandTextArea)
        inp.text = "/туц x"
        await pilot.pause()
        assert inp.text == "/туц x"


@pytest.mark.asyncio
async def test_undo_returns_typed_word(tmp_path):
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        inp = pilot.app.screen.query_one("#cmdinput", CommandTextArea)
        inp.focus()
        await _type(pilot, inp, "/туц ")
        assert inp.text == "/new "
        inp.action_undo()
        await pilot.pause()
        assert inp.text == "/туц "
        # Набранное остаётся: следующий пробел слово уже не заменяет.
        await _type(pilot, inp, " ")
        assert inp.text == "/туц  "


@pytest.mark.asyncio
async def test_typing_after_replacement_is_undone_separately(tmp_path):
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        inp = pilot.app.screen.query_one("#cmdinput", CommandTextArea)
        inp.focus()
        await _type(pilot, inp, "/туц ")
        await _type(pilot, inp, "x")
        inp.action_undo()
        await pilot.pause()
        assert inp.text == "/new "


@pytest.mark.asyncio
async def test_skill_catalog_built_once_per_cache(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.screens import main as main_mod

    built = []
    real = main_mod.catalog_from_entries

    def counting(entries):
        built.append(1)
        return real(entries)

    monkeypatch.setattr(main_mod, "catalog_from_entries", counting)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = list(_CACHE)
        before = len(built)
        for _ in range(3):
            scr._is_known_command("ьщвуд")
            scr._is_known_command("икфштыещкьштп")
        assert len(built) - before == 1
        scr._skills_cache = list(_CACHE)
        scr._is_known_command("ьщвуд")
        assert len(built) - before == 2


@pytest.mark.asyncio
async def test_alias_of_skill_command_opens_skill_picker(tmp_path):
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = list(_CACHE)
        inp = scr.query_one("#cmdinput", CommandTextArea)
        inp.text = "/навык "
        await pilot.pause()
        assert scr._skill_picker is not None


@pytest.mark.asyncio
async def test_skill_typed_exactly_wins_over_command_in_other_layout(
    tmp_path,
):
    """`ыефегы` — это `status` в чужой раскладке, но есть скилл с таким
    именем: набранное как есть важнее перевода."""
    pytest.importorskip("textual")
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "ыефегы"}]
        assert scr._typed_name("ыефегы") == "ыефегы"
        assert scr._typed_name("ьщвуд") == "model"
