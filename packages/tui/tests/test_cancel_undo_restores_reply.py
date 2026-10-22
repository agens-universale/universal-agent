"""Откат по Esc возвращает в поле реплику, которую снимает, — тем же
`plain_text`, что и ↑, стоп и конец хода: скилл не теряется."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from gc_core.attachments.model import chip_text
from gc_tui.app import TuiApp
from gc_tui.widgets.command_input import CommandTextArea
from gc_tui.widgets.user_reply import UserReplyContainer
from image_helpers import make_attachment_file


class _Runner:
    def is_running(self) -> bool:
        return True

    def stop(self) -> None:
        pass


async def _undo(pilot):
    scr = pilot.app.screen
    pilot.app.active_runner = _Runner()
    scr._events_since_last_user_send = 0
    pilot.app.query_one("#cmdinput").text = ""
    await pilot.press("escape")
    await pilot.pause()
    return pilot.app.query_one("#cmdinput", CommandTextArea)


@pytest.mark.asyncio
async def test_skill_reply_comes_back_with_the_skill():
    async with TuiApp().run_test() as pilot:
        pilot.app.screen._mount_user_reply(
            "тест", state="pending", skill="supercoder:brainstorming"
        )
        inp = await _undo(pilot)
        assert inp.text == "/supercoder:brainstorming тест"
        assert not list(pilot.app.screen.query(UserReplyContainer))


@pytest.mark.asyncio
async def test_plain_reply_comes_back():
    async with TuiApp().run_test() as pilot:
        pilot.app.screen._mount_user_reply("привет", state="pending")
        assert (await _undo(pilot)).text == "привет"


@pytest.mark.asyncio
async def test_undo_restores_the_reply_it_removes_not_a_withdrawn_one():
    """Отправил A, B в очередь, B забрал ↑ и очистил поле: откат снимает A
    и возвращает текст A."""
    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._mount_user_reply("A", state="pending")
        scr._last_user_send_text = "B"  # устаревшая копия — не источник
        inp = await _undo(pilot)
        assert inp.text == "A"


@pytest.mark.asyncio
async def test_reply_with_image_restores_the_image(tmp_path):
    async with TuiApp().run_test() as pilot:
        att = make_attachment_file(1, tmp_path)
        text = f"глянь {chip_text(1)}"
        pilot.app.screen._mount_user_reply(
            text, state="pending", images=(att,)
        )
        inp = await _undo(pilot)
        assert inp.text == text
        assert inp.images_in_buffer() == (att,)
