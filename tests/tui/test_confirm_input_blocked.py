"""Баг: при активном confirm-баннере печатные клавиши (y/s/n) утекали в инпут.

Если CommandTextArea сохраняла фокус, «y» печаталось в неё (а Enter отправлял
сообщение) — confirm не отвечался. Инпут на время confirm должен быть НЕ
способен принимать клавиши (не только blur, но disabled)."""
import pytest


@pytest.mark.asyncio
async def test_confirm_y_answers_when_input_was_focused(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_input import CommandTextArea

    answered = []

    class _Runner:
        def is_running(self):
            return True

        def submit_confirm_reply(self, choice):
            answered.append(choice)

        def cancel_confirm(self):
            pass

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app.active_runner = _Runner()

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        inp = scr.query_one("#cmdinput", CommandTextArea)
        inp.focus()  # симулируем: инпут в фокусе на момент показа баннера
        await pilot.pause()

        scr.show_confirm_banner("Защита записи", "Разрешить?", [("path", "/x")])
        await pilot.pause()

        await pilot.press("y")
        await pilot.pause()

        assert answered == ["once"], f"'y' должен ОТВЕТИТЬ confirm, а не уйти в инпут; answered={answered}"
        assert inp.text == "", f"'y' не должен печататься в инпут; text={inp.text!r}"
