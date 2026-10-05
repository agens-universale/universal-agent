"""Submitted несёт вложения сообщения: image-плашки буфера, снятые в момент Enter — раньше,
чем экран очистит поле (очистка сбрасывает стор плашек)."""

import pytest

from gc_core.attachments.model import chip_text
from image_helpers import make_attachment
from gc_testkit.tui_support import tui_settings


def test_submitted_defaults_to_no_attachments():
    from gc_tui.widgets.command_input import CommandTextArea

    assert CommandTextArea.Submitted(value="x").attachments == ()


@pytest.mark.asyncio
async def test_enter_captures_images_before_the_screen_clears_the_input(
    tmp_path, monkeypatch
):
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.widgets.command_input import CommandTextArea

    class _Fake:  # no-op executor: Enter не строит настоящий
        async def arun(self, messages, *, thread_id, **kw):
            pass

    seen = []
    handler = TraceScreen.on_command_text_area_submitted

    async def spy(self, event):
        seen.append(event)
        await handler(self, event)

    monkeypatch.setattr(TraceScreen, "on_command_text_area_submitted", spy)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        inp = pilot.app.screen.query_one("#cmdinput", CommandTextArea)
        a, b = make_attachment("a" * 16, 1), make_attachment("b" * 16, 2)
        inp.insert_image_chips([a, b], 1)
        inp.insert("глянь, и снова ")  # пробел перед текстом — сам
        inp.insert_image_chips(
            [make_attachment("a" * 16, 3)], 3
        )  # та же картинка снова
        await pilot.press("enter")
        await pilot.pause()
        assert inp.text == ""  # экран очистил поле…
        assert (
            inp._chips.images_in_text(chip_text(1)) == []
        )  # …и стор плашек сброшен
        (event,) = seen
        assert (
            event.value
            == f"{chip_text(1)} {chip_text(2)} глянь, и снова {chip_text(1)}"
        )
        assert event.attachments == (a, b)
