"""Откат по Esc возвращает в поле реплику, которую снимает, — тем же
`plain_text`, что и ↑, стоп и конец хода: скилл не теряется."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("textual")

from gc_core.attachments.model import chip_text
from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.widgets.command_input import CommandTextArea
from gc_tui.widgets.user_reply import UserReplyContainer
from image_helpers import make_attachment_file


class _Runner:
    def is_running(self) -> bool:
        return True

    def produced_output(self) -> bool:
        return False

    def stop(self) -> None:
        pass


async def _undo(pilot):
    scr = pilot.app.screen
    pilot.app.active_runner = _Runner()
    scr._last_user_message_id = "m_undo"  # отправка, агент ничего не выдал
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


class _QueueRunner(_Runner):
    """Идущий ход с очередью: B ложится в неё и снимается ↑."""

    def __init__(self) -> None:
        self.added: list[dict] = []

    def is_child_input_active(self) -> bool:
        return False

    def add_message(self, message: dict) -> None:
        self.added.append(message)

    def withdraw_message(self, msg_id: str) -> bool:
        return True


@pytest.mark.asyncio
async def test_undo_restores_the_reply_it_removes_not_a_withdrawn_one(
    tmp_path,
):
    """Отправил A, B в очередь, B забрал ↑ и очистил поле: откат снимает A
    и возвращает текст A."""
    tid = "u" * 32
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")
    meta = {"thread_id": tid, "messages_count": 0, "tui": {}}
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _QueueRunner()
        pilot.app.active_runner = runner
        scr._mount_user_reply("A", state="pending")
        await scr.on_command_text_area_submitted(
            CommandTextArea.Submitted(value="B", attachments=())
        )
        await pilot.pause()
        assert [m["content"] for m in runner.added] == ["B"]
        inp = pilot.app.query_one("#cmdinput", CommandTextArea)
        inp.focus()
        await pilot.press("up")
        await pilot.pause()
        assert inp.text == "B"
        inp.text = ""
        await pilot.press("escape")
        await pilot.pause()
        assert inp.text == "A"
        assert not list(scr.query(UserReplyContainer))


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
