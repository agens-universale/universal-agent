"""Баг: при активном confirm-баннере печатные клавиши (y/s/n) утекали в инпут.

Если CommandTextArea сохраняла фокус, «y» печаталось в неё (а Enter отправлял
сообщение) — confirm не отвечался. Инпут на время confirm должен быть НЕ
способен принимать клавиши (не только blur, но disabled)."""
import pytest


@pytest.mark.asyncio
async def test_confirm_y_answers_when_input_was_focused(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea

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

        assert answered == ["allow_remember"], f"'y' должен ОТВЕТИТЬ confirm, а не уйти в инпут; answered={answered}"
        assert inp.text == "", f"'y' не должен печататься в инпут; text={inp.text!r}"


@pytest.mark.asyncio
async def test_confirm_disables_input_synchronously(tmp_path):
    """Инвариант против гонки фокуса: show_confirm_banner ДОЛЖЕН синхронно
    отключить инпут (disabled=True), а не полагаться на асинхронный set_focus(None).

    Иначе под нагрузкой (агент пишет, медленная модель) первый y успевает уйти
    в ещё-сфокусированный инпут до применения blur — «плавающий» баг."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        inp = scr.query_one("#cmdinput", CommandTextArea)
        inp.focus()
        await pilot.pause()

        scr.show_confirm_banner("Защита записи", "Разрешить?", [("path", "/x")])
        # БЕЗ pause — проверяем синхронное состояние сразу после показа баннера
        assert inp.disabled is True, "инпут должен быть отключён синхронно при показе confirm"

        scr._hide_confirm_banner()
        assert inp.disabled is False, "инпут должен снова включиться после снятия баннера"
