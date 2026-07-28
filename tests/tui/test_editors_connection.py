import pytest
pytest.importorskip("textual")

from textual.app import App, ComposeResult
from textual.widgets import Input


def _harness(editor):
    class _App(App):
        def compose(self) -> ComposeResult:
            yield editor
    return _App()


async def test_url_prefilled_token_empty():
    """Адрес предзаполнен сырым значением; поле токена пустое (секрет не заезжает)."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://old/v1/chat/completions",
                          auth_header="Bearer sk-SECRET")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        url_val = ed.query_one("#conn-url", Input).value
        tok_val = ed.query_one("#conn-token", Input).value
        assert url_val == "http://old/v1/chat/completions"
        assert tok_val == ""                       # токен НЕ предзаполнен
        rows = " ".join(str(w.render()) for w in ed.query(".onb-option"))
        assert "sk-SECRET" not in rows             # литерал не светится, только маска


async def test_submit_empty_url_returns_error():
    """Пустой адрес → error «введите адрес» (пункт C), не «ничего не изменено»."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://old/v1", auth_header="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-url", Input).value = ""
        url, header, error, _proxy = ed.submit()
        assert url is None and error == "введите адрес"


async def test_submit_malformed_url_returns_error():
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://old/v1", auth_header="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-url", Input).value = "notaurl"
        url, header, error, _proxy = ed.submit()
        assert url is None and error == "нужны http(s):// и хост"


async def test_submit_valid_pair():
    """Валидный адрес + токен → (url, header, None)."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://old/v1/chat/completions", auth_header="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-url", Input).value = "https://new/v1/chat/completions"
        ed.move(1)                                  # курсор → Bearer, поле токена показано
        await pilot.pause()
        ed.query_one("#conn-token", Input).value = "sk-NEW"
        url, header, error, _proxy = ed.submit()
        assert url == "https://new/v1/chat/completions"
        assert header == "Bearer sk-NEW"
        assert error is None


async def test_bearer_selected_without_token_asks_for_token():
    """Выбрали Bearer (курсор на нём), токен не введён, действующего токена нет →
    submit просит «введите токен», а не молчит «ничего не изменено»."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://h/v1", auth_header="")  # действует «без авторизации»
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)                                   # курсор → Bearer, токен пуст
        url, header, error, _proxy = ed.submit()
        assert error == "введите токен"
        assert url is None and header is None
        assert ed.is_dirty() is True                 # незавершённый выбор — секция «изменена»


async def test_bearer_active_empty_field_is_noop():
    """Действует Bearer с токеном; зашли курсором на Bearer, поле пусто → submit ничего не
    меняет (не ошибка): действующий токен остаётся, «ничего не изменено» здесь корректно."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://h/v1", auth_header="Bearer sk-OLD")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)                                   # курсор → Bearer, поле пусто
        url, header, error, _proxy = ed.submit()
        assert (url, header, error) == (None, None, None)
        assert ed.is_dirty() is False


async def test_is_dirty_false_on_entry():
    """Вход без правок — не dirty (адрес предзаполнен, токен пуст = «не менять»)."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://old/v1", auth_header="Bearer x")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ed.is_dirty() is False


async def test_move_does_not_move_active_mark_before_enter():
    """Навигация курсором НЕ двигает метку ● (действующий вариант из конфига) — как на
    «Модели»: едут только курсор ▸ и подсветка, выбор фиксирует submit/peek по позиции."""
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Static
    ed = ConnectionEditor(url_current="http://old/v1", auth_header="")  # действует «без авторизации»
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)                                  # курсор → Bearer, БЕЗ ввода токена
        await pilot.pause()
        opt0 = str(ed.query_one("#conn-opt-0", Static).render())   # Bearer
        opt1 = str(ed.query_one("#conn-opt-1", Static).render())   # без авторизации
        assert "▸" in opt0                          # курсор переехал на Bearer
        assert "●" in opt1 and "○" in opt0          # но метка ● осталась на действующем варианте
        _url, auth_idx, _tok, _proxy = ed.peek()
        assert auth_idx == 0                        # Enter зафиксирует то, что под курсором (Bearer)


async def test_navigation_only_does_not_change_selection():
    """Проход курсором по вариантам и обратно (без Enter/ввода токена) не делает секцию
    «изменено» и не двигает действующий выбор — установка только по submit (жалоба со скрина)."""
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Static
    ed = ConnectionEditor(url_current="http://old/v1", auth_header="Bearer x")  # действует Bearer
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1); ed.move(1); ed.move(1)          # Bearer → без авторизации → назад на адрес
        await pilot.pause()
        assert ed.is_dirty() is False               # ничего не выбирали/не вводили
        opt0 = str(ed.query_one("#conn-opt-0", Static).render())
        assert "●" in opt0                          # метка ● осталась на действующем Bearer


async def test_focus_first_returns_to_address():
    """focus_first после навигации возвращает фокус на адрес; на опциях не остаётся
    призрака курсора ▸ и подсветки (адрес — через фокус поля, опции — через ▸/selected)."""
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Static, Input
    ed = ConnectionEditor(url_current="http://old/v1", auth_header="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1); ed.move(1)                      # курсор ушёл на опции
        await pilot.pause()
        ed.focus_first()
        await pilot.pause()
        opt0 = ed.query_one("#conn-opt-0", Static)
        opt1 = ed.query_one("#conn-opt-1", Static)
        assert "▸" not in str(opt0.render()) and "▸" not in str(opt1.render())   # нет призрака курсора
        assert not opt0.has_class("selected") and not opt1.has_class("selected")  # нет призрака подсветки
        assert ed.query_one("#conn-url", Input).has_focus                          # фокус вернулся на адрес


async def test_token_field_placeholder_shows_masked_current():
    """Действующий токен виден МАСКОЙ в placeholder поля (поле не выглядит пустым),
    но НЕ заезжает в значение — секретная безопасность сохранена."""
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    ed = ConnectionEditor(url_current="http://h/v1", auth_header="Bearer sk-SECRETVALUE")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        tok = ed.query_one("#conn-token", Input)
        assert tok.value == ""                        # значение пустое — секрет не заезжает
        assert "SECRETVALUE" not in tok.placeholder   # чувствительная часть под маской
        assert "***" in tok.placeholder               # но поле визуально не пустое


async def test_selected_class_highlights_active_option():
    """Строка выбранного варианта авторизации получает класс selected (фон-подсветка)."""
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Static
    ed = ConnectionEditor(url_current="http://h/v1", auth_header="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)                                    # курсор на Bearer
        await pilot.pause()
        assert ed.query_one("#conn-opt-0", Static).has_class("selected")
        assert not ed.query_one("#conn-opt-1", Static).has_class("selected")
