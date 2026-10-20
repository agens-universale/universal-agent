"""Подсказка F1 в пустом поле ввода — раз за запуск, до первого действия."""

import pytest
from gc_testkit.tui_support import tui_settings

pytestmark = pytest.mark.asyncio

_HINTED = "опишите задачу или /команда · F1 — справка"
_READY = "опишите задачу или /команда"


def _app(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


def _placeholder(pilot) -> str:
    return pilot.app.screen_stack[0].query_one("#cmdinput").placeholder


async def test_launch_shows_the_hint_until_the_first_message(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        assert _placeholder(pilot) == _HINTED
        pilot.app.screen._on_user_text_submitted("привет")
        await pilot.pause()
        assert _placeholder(pilot) == _READY


async def test_help_spends_the_hint_for_the_whole_launch(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("f1")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert _placeholder(pilot) == _READY


async def test_palette_spends_the_hint(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        pilot.app.screen._open_palette(prefix="")
        await pilot.pause()
        pilot.app.screen._close_palette()
        assert _placeholder(pilot) == _READY


async def test_new_session_of_the_same_launch_has_plain_text(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert _placeholder(pilot) == _READY


class _AskingRunner:
    """Прогон, ждущий ответа на вопрос агента."""

    def __init__(self):
        self.submitted: list = []

    def is_running(self) -> bool:
        return True

    def submit_ask_human_reply(self, answer) -> None:
        self.submitted.append(answer)


async def test_answer_to_the_agent_question_spends_the_hint(tmp_path):
    """Ответ на вопрос агента — тоже отправленное сообщение."""
    from gc_tui.models import TraceEvent
    from gc_tui.widgets.command_input import CommandTextArea

    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = pilot.app.tui_state.active_task_id = "a" * 32
        runner = _AskingRunner()
        pilot.app.active_runner = runner
        scr._on_ask_human_event(
            TraceEvent(
                no=1,
                event_ts_utc_iso="2026-09-24T10:00:00+00:00",
                kind="ask_human",
                title="ask_human",
                summary="Какой файл?",
                full_payload={},
                message_id="ah_1",
                call_id="call_1",
                tool_name="ask_human",
            ),
            live=True,
        )
        await pilot.pause()
        assert scr._help_hint_spent is False
        await scr.on_command_text_area_submitted(
            CommandTextArea.Submitted(value="main.py", attachments=())
        )
        await pilot.pause()
        assert runner.submitted == ["main.py"]
        assert scr._help_hint_spent is True
        assert _placeholder(pilot) == _READY
