import pytest
pytest.importorskip("textual")

from textual.app import App, ComposeResult
from textual.widgets import Input, Static


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
    """Валидный адрес + токен (галка ○, нет активной авторизации) → (url, header, None)."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://old/v1/chat/completions", auth_header=None)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-url", Input).value = "https://new/v1/chat/completions"
        ed.move(1)                                  # курсор → токен
        await pilot.pause()
        ed.query_one("#conn-token", Input).value = "sk-NEW"
        url, header, error, _proxy = ed.submit()
        assert url == "https://new/v1/chat/completions"
        assert header == "Bearer sk-NEW"
        assert error is None


async def test_bearer_selected_without_token_asks_for_token():
    """Действовала «без авторизации»; галку сняли (Space, выбран Bearer), токен не
    введён → submit просит «введите токен», а не молчит «ничего не изменено»."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://h/v1", auth_header="")  # действует «без авторизации»
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)                # курсор → галка (замороженный токен пропускается при ●)
        assert ed.toggle_no_auth() is True            # ● -> ○ (выбран Bearer)
        url, header, error, _proxy = ed.submit()
        assert error == "введите токен"
        assert url is None and header is None
        assert ed.is_dirty() is True                 # незавершённый выбор — секция «изменена»


async def test_bearer_active_empty_field_is_noop():
    """Действует Bearer с токеном; галка не тронута (○), поле пусто → submit ничего
    не меняет (не ошибка): действующий токен остаётся."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://h/v1", auth_header="Bearer sk-OLD")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)                                   # курсор → токен, поле пусто
        url, header, error, _proxy = ed.submit()
        assert (url, header, error) == (None, None, None)
        assert ed.is_dirty() is False


async def test_no_auth_toggle_writes_empty_header():
    """Действовал Bearer; галку включили (Space, ●) → submit снимает авторизацию."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://h/v1", auth_header="Bearer sk-OLD")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1); ed.move(1)                       # курсор → галка
        assert ed.toggle_no_auth() is True            # ○ -> ●
        url, header, error, _proxy = ed.submit()
        assert (url, error) == (None, None)
        assert header == ""


async def test_no_auth_already_active_unchanged_is_noop():
    """Уже действовала «без авторизации» (галка ● по умолчанию, не тронута) →
    submit ничего не пишет (header None), не переписывает "" заново."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://h/v1", auth_header="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
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


async def test_navigation_past_galka_does_not_toggle():
    """Проход курсором по позициям (без Space) не переключает галку и не делает
    секцию «изменено» — переключение только явным toggle_no_auth()/Space."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://old/v1", auth_header="Bearer x")  # действует Bearer, галка ○
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1); ed.move(1); ed.move(1)          # токен → галка → прокси
        await pilot.pause()
        assert ed.is_dirty() is False
        noauth = str(ed.query_one("#conn-noauth", Static).render())
        assert "○" in noauth and "●" not in noauth   # галка не тронута


async def test_return_to_address_clears_the_checkbox_ghost():
    """Курсор вернулся на адрес: фокус в поле, на галке не осталось призрака подсветки.

    Проверяется продакшен-путём — лентой (`move`), как её водят экраны. Раньше тут
    звали `focus_first()`, но в проде его не вызывал никто: он повторял приземление
    `move` и был удалён (2026-08-07)."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://old/v1", auth_header="Bearer x")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1); ed.move(1)                      # курсор ушёл на галку
        await pilot.pause()
        assert ed.query_one("#conn-noauth", Static).has_class("selected")   # предусловие
        ed.move(-1); ed.move(-1)                    # и вернулся на адрес
        await pilot.pause()
        assert not ed.query_one("#conn-noauth", Static).has_class("selected")
        assert ed.query_one("#conn-url", Input).has_focus   # фокус вернулся на адрес


async def test_token_field_placeholder_shows_masked_current():
    """Действующий токен виден МАСКОЙ в placeholder поля (поле не выглядит пустым),
    но НЕ заезжает в значение — секретная безопасность сохранена."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://h/v1", auth_header="Bearer sk-SECRETVALUE")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        tok = ed.query_one("#conn-token", Input)
        assert tok.value == ""                        # значение пустое — секрет не заезжает
        assert "SECRETVALUE" not in tok.placeholder   # чувствительная часть под маской
        assert "***" in tok.placeholder               # но поле визуально не пустое


async def test_selected_class_highlights_galka_row():
    """Строка галки получает класс selected, когда курсор на ней (фон-подсветка)."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://h/v1", auth_header="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)                # курсор на галке (замороженный токен пропускается при ●)
        await pilot.pause()
        assert ed.query_one("#conn-noauth", Static).has_class("selected")


async def test_toggle_no_auth_off_cursor_is_noop_and_does_not_stop():
    """Space вне позиции галки — toggle_no_auth() возвращает False (хост не стопит клавишу)."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://h/v1", auth_header=None)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ed.toggle_no_auth() is False           # курсор на url (idx 0)
        ed.move(1)                                     # → token
        assert ed.toggle_no_auth() is False


async def test_toggle_no_auth_freezes_token_input_empty():
    """Space на галке замораживает #conn-token (смок 2026-07-30): поле на месте
    (раскладка не прыгает), но disabled, пустое и без placeholder-хинта."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://h/v1", auth_header=None)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-token", Input).value = "typed"     # введённое очищается при ●
        ed.move(1); ed.move(1)                        # курсор → галка
        assert ed.toggle_no_auth() is True
        await pilot.pause()
        tok = ed.query_one("#conn-token", Input)
        assert tok.display is True                     # поле не исчезает
        assert tok.disabled is True
        assert tok.value == "" and tok.placeholder == "не используется"


async def test_toggle_no_auth_twice_restores_token_input():
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="http://h/v1", auth_header=None)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1); ed.move(1)
        ed.toggle_no_auth()
        ed.toggle_no_auth()
        await pilot.pause()
        tok = ed.query_one("#conn-token", Input)
        assert tok.disabled is False                   # поле ожило
        assert tok.placeholder == "введите токен"      # хинт вернулся


async def test_create_mode_has_no_galka_and_create_hint():
    """create_mode=True (Task 8, онбординг): без галки «без авторизации», позиции
    0=url,1=token,2=proxy; хинт пустого токена — В PLACEHOLDER поля (не отдельной строкой)."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="", auth_header=None, create_mode=True)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not ed.query(".onb-option")                 # ни одной галки/опции
        ph = ed.query_one("#conn-token", Input).placeholder
        assert ph == "если не указать — запрос пойдёт без токена (подходит, если сервер его не требует)"
        ed.move(1); ed.move(1)                              # url -> token -> proxy (3 позиции)
        await pilot.pause()
        assert ed.query_one("#conn-proxy", Input).has_focus


async def test_create_mode_empty_token_submits_without_header():
    """create_mode: пустой токен → header None, никакой ошибки (в отличие от settings)."""
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="", auth_header=None, create_mode=True)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-url", Input).value = "http://h/v1/chat/completions"
        url, header, error, _proxy = ed.submit()
        assert error is None
        assert header is None
        assert url == "http://h/v1/chat/completions"


async def test_create_mode_empty_url_shows_variant_preset_hint():
    """Minor-6: онбординг (create_mode, поле адреса пустое) — первичный хинт
    пресет-листа отличается от канонного settings-варианта (спека §2.1)."""
    from tui.editors.connection import ConnectionEditor
    from config_modules.endpoint_presets import default_endpoint_presets
    ed = ConnectionEditor(url_current="", auth_header=None, create_mode=True,
                          presets=default_endpoint_presets())
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = str(ed.query_one(".conn-url-preset-hint").render())
        assert status == "выберите провайдера или впишите свой адрес · Enter — подставить"


async def test_edit_mode_prefilled_url_shows_canon_preset_hint():
    """Хинт пресет-листа при непустом адресе (settings/edit) остаётся канонным —
    когда адрес совпадает с одним из пресетов (список раскрывается, подсвечивая
    ЕГО; отклонение от источника, спека-отклонение №5: список не раскрывается
    поверх СВОЕГО адреса, которого нет в пресетах — см. test_editors_connection
    рядом и test_preset_list.py)."""
    from tui.editors.connection import ConnectionEditor
    from config_modules.endpoint_presets import default_endpoint_presets
    ed = ConnectionEditor(url_current="https://openrouter.ai/api/v1/chat/completions",
                          auth_header=None, presets=default_endpoint_presets())
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = str(ed.query_one(".conn-url-preset-hint").render())
        assert status == "пресеты · печать — фильтр/свой адрес · Enter — подставить · Esc — свернуть"


async def test_arrow_leaves_the_address_when_the_filter_matches_no_preset():
    """Набран свой адрес — в пресет-листе не осталось ни строки: водить по нему
    нечем, и забирать стрелку значит запереть курсор на адресе без всякой обратной
    связи (контракт `FieldListController.move`, дословно). Стрелка возвращается
    ленте формы, а список сворачивается — курсор ушёл с поля адреса."""
    from config_modules.endpoint_presets import default_endpoint_presets
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="", auth_header=None,
                          presets=default_endpoint_presets())
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ed.presets_open
        ed.query_one("#conn-url", Input).value = "https://my-own-host.internal/v1"
        await pilot.pause()
        assert ed.presets_open and not ed.presets_have_selection
        ed.move(1)
        await pilot.pause()
        assert ed.query_one("#conn-token", Input).has_focus
        assert not ed.presets_open
        # Набранное осталось в поле: пресет-лист адреса печать не откатывает
        # (`restore_on_close=False`) — она тут ввод своего значения, а не фильтр.
        assert ed.query_one("#conn-url", Input).value == "https://my-own-host.internal/v1"


async def test_arrow_keeps_walking_the_preset_list_while_it_has_rows():
    """Пока в списке есть что выбирать, стрелка — его: курсор формы остаётся на
    адресе до Enter/Esc (модальность списка, канон §4.2.2)."""
    from config_modules.endpoint_presets import default_endpoint_presets
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="", auth_header=None,
                          presets=default_endpoint_presets())
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)
        await pilot.pause()
        assert ed.presets_open
        assert ed.query_one("#conn-url", Input).has_focus


async def test_create_mode_typed_token_becomes_bearer_header():
    from tui.editors.connection import ConnectionEditor
    ed = ConnectionEditor(url_current="", auth_header=None, create_mode=True)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-url", Input).value = "http://h/v1/chat/completions"
        ed.move(1)                                          # → token
        ed.query_one("#conn-token", Input).value = "sk-NEW"
        url, header, error, _proxy = ed.submit()
        assert error is None
        assert header == "Bearer sk-NEW"


async def test_preset_list_opens_whole_not_filtered_by_the_prefilled_address():
    """Вход на поле адреса раскрывает ВЕСЬ список, курсор — на совпавшем пресете.

    Живой смок 2026-08-04: в «Подключении» список показывал одну строку, а стоило
    уйти с поля и вернуться — все три. Сужал его `Input.Changed`, который Textual
    постит на поле, смонтированное СО значением: список к тому времени уже раскрыт
    (канон §2.1), и хозяин честно принимает событие за печать. Задумано иначе —
    докстринг `can_open`/`initial_index` прямо говорит «раскрываем и подсвечиваем
    ИМЕННО его»."""
    from config_modules.endpoint_presets import default_endpoint_presets
    from tui.editors.connection import ConnectionEditor
    presets = default_endpoint_presets()
    ed = ConnectionEditor(url_current=presets[1].url, auth_header=None, presets=presets)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ed.query_one("#conn-url", Input).value == presets[1].url
        visible = [w for w in ed.query(".conn-preset-row") if w.display]
        assert len(visible) == len(presets), (
            f"список раскрылся суженным: {[str(w.render()) for w in visible]}")
        assert str(visible[1].render()).startswith("▸ "), "▸ не на совпавшем пресете"
        # Печать поверх — уже настоящий фильтр, а не «значение поля».
        ed.query_one("#conn-url", Input).value = presets[0].name.lower()
        await pilot.pause()
        assert len([w for w in ed.query(".conn-preset-row") if w.display]) == 1


async def test_section_entry_shows_the_whole_preset_list(tmp_path):
    """Живой смок 2026-08-04: вход в «Подключение» показывал ОДНУ строку — совпавший
    пресет, — а стоило уйти с поля и вернуться, как появлялись все три. Задумано
    иначе (докстринг `can_open`/`initial_index`: «раскрываем и подсвечиваем ИМЕННО
    его»): список полный, курсор — на совпавшем.

    Через настоящую секцию, а не через контроллер: сужал список `Input.Changed`,
    который постит смонтированное со значением поле, — на минимальном хосте такого
    события нет вовсе."""
    from config_modules.endpoint_presets import DEFAULT_ENDPOINT_PRESETS
    from tests.tui.conftest import settings_connection
    from tui.editors.connection import ConnectionEditor
    async with settings_connection(tmp_path, url=DEFAULT_ENDPOINT_PRESETS[1].url) as (_pilot, scr):
        ed = scr.query_one(ConnectionEditor)
        rows = list(ed.query(".conn-preset-row"))
        visible = [w for w in rows if w.display]
        assert len(visible) == len(DEFAULT_ENDPOINT_PRESETS), (
            f"список раскрылся суженным: {[str(w.render()) for w in visible]}")
        assert str(visible[1].render()).startswith("▸ "), "▸ не на совпавшем пресете"
