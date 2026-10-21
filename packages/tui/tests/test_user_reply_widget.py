"""Реплика пользователя (спека P3 §2.2): маркер `› ` в колонке `●`, подложка
во всю ширину ленты без кромки, строки следствий `└` — под подложкой."""

import pytest

pytest.importorskip("textual")

from rich.color import Color as RichColor
from rich.style import Style
from rich.text import Text
from textual.color import Color

from gc_core.attachments.model import chip_text
from gc_testkit.tui_support import tui_settings
from gc_tui.palette import ACCENT, BG, DIM, ERR, FOCUS_BG, USER_BAND_BG
from gc_tui.widgets.trace_row import TraceRow
from gc_tui.widgets.user_reply import UserReplyContainer
from image_helpers import make_attachment_file


def _band(reply):
    return reply.query_one(".user-band")


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


async def _mount(pilot, *widgets):
    trace = pilot.app.query_one("#trace")
    for widget in widgets:
        await trace.mount(widget)
    await pilot.pause()


def _color_at(text: Text, index: int):
    styles = (
        Style.parse(s.style) if isinstance(s.style, str) else s.style
        for s in text.spans
        if s.start <= index < s.end
    )
    return {style.color for style in styles if style.color}


def test_marker_on_the_first_line_only_in_accent():
    rendered = UserReplyContainer("первая строка\nвторая")._band.render()
    assert rendered.plain.split("\n") == ["› первая строка", "  вторая"]
    assert _color_at(rendered, 0) == {RichColor.parse(ACCENT)}


def test_failed_marker_is_red_without_bang():
    reply = UserReplyContainer("упало", state="failed")
    rendered = reply._band.render()
    assert rendered.plain == "› упало"
    assert _color_at(rendered, 0) == {RichColor.parse(ERR)}


def test_set_state_failed_recolors_the_marker():
    reply = UserReplyContainer("x", state="pending")
    reply.set_state("failed")
    assert reply.state == "failed"
    assert _color_at(reply._band.render(), 0) == {RichColor.parse(ERR)}


def test_pending_looks_like_normal():
    pending = UserReplyContainer("x", state="pending")
    normal = UserReplyContainer("x")
    assert pending._band.render() == normal._band.render()
    assert pending.classes == normal.classes
    assert pending._band.classes == normal._band.classes


def test_brackets_not_interpreted_as_markup():
    reply = UserReplyContainer("check line [42] of [bold]file[/bold]")
    assert reply._band.render().plain == (
        "› check line [42] of [bold]file[/bold]"
    )


@pytest.mark.asyncio
async def test_marker_in_bullet_column_band_full_width_no_edge(tmp_path):
    agent = TraceRow(prefix="● ", content="ответ", kind_class="line-agent")
    reply = UserReplyContainer("слово " * 40)
    async with _app(tmp_path).run_test(size=(80, 30)) as pilot:
        await _mount(pilot, agent, reply)
        band = _band(reply)
        bullet = agent.query_one(".trace-prefix").content_region.x
        assert band.content_region.x == bullet
        assert band.region.width == agent.region.width
        assert band.styles.background == Color.parse(USER_BAND_BG)
        assert band.styles.border_left[0] == ""
        lines = band.render().plain.split("\n")
        assert len(lines) > 1 and lines[0].startswith("› ")
        assert all(line.startswith("  слово") for line in lines[1:])


@pytest.mark.asyncio
async def test_image_lines_stay_inside_the_band(tmp_path):
    att = make_attachment_file(4, tmp_path)
    reply = UserReplyContainer(chip_text(4), images=(att,))
    async with _app(tmp_path).run_test(size=(80, 30)) as pilot:
        await _mount(pilot, reply)
        band = _band(reply)
        assert band.render().plain.split("\n") == [
            f"› {chip_text(4)}",
            f"  └ {chip_text(4)} · скрин.png",
        ]
        assert band.region.height == reply.region.height == 2


@pytest.mark.asyncio
async def test_consequence_line_sits_under_the_band(tmp_path):
    reply = UserReplyContainer("привет")
    reply.add_consequence(Text("до монтирования"))
    async with _app(tmp_path).run_test(size=(80, 30)) as pilot:
        await _mount(pilot, reply)
        reply.add_consequence(Text("скилл не найден", style=ERR))
        await pilot.pause()
        band = _band(reply)
        line = reply.query_one(".reply-consequence")
        assert line.region.y == band.region.bottom
        assert line.region.height == 2
        assert line.styles.background != Color.parse(USER_BAND_BG)
        assert pilot.app.query_one("#trace").styles.background == Color.parse(
            BG
        )
        shown = line.content
        assert shown.plain == "  └ до монтирования\n  └ скилл не найден"
        assert _color_at(shown, 2) == {RichColor.parse(DIM)}


@pytest.mark.asyncio
async def test_no_consequence_line_without_consequences(tmp_path):
    reply = UserReplyContainer("привет")
    async with _app(tmp_path).run_test(size=(80, 30)) as pilot:
        await _mount(pilot, reply)
        assert reply.region.height == 1


@pytest.mark.asyncio
async def test_focus_paints_the_band(tmp_path):
    reply = UserReplyContainer("привет")
    async with _app(tmp_path).run_test(size=(80, 30)) as pilot:
        await _mount(pilot, reply)
        reply.focus()
        await pilot.pause()
        assert _band(reply).styles.background == Color.parse(FOCUS_BG)


@pytest.mark.asyncio
async def test_select_all_copies_text_without_marker_and_consequence(
    tmp_path,
):
    reply = UserReplyContainer("первая\nвторая")
    async with _app(tmp_path).run_test(size=(80, 30)) as pilot:
        await _mount(pilot, reply)
        reply.add_consequence(Text("следствие"))
        await pilot.pause()
        reply.text_select_all()
        await pilot.pause()
        assert pilot.app.screen.get_selected_text() == "первая\nвторая"


@pytest.mark.asyncio
async def test_input_prompt_is_the_reply_marker(tmp_path):
    async with _app(tmp_path).run_test(size=(80, 30)) as pilot:
        prompt = pilot.app.query_one("#input-prompt")
        assert str(prompt.render()) == "› "
