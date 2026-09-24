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
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://old/v1/chat/completions",
        auth_header="Bearer sk-SECRET",
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        url_val = ed.query_one("#conn-url", Input).value
        tok_val = ed.query_one("#conn-token", Input).value
        assert url_val == "http://old/v1/chat/completions"
        assert tok_val == ""  # токен НЕ предзаполнен
        rows = " ".join(str(w.render()) for w in ed.query(".onb-option"))
        assert "sk-SECRET" not in rows  # литерал не светится, только маска


async def test_submit_empty_url_returns_error():
    """Пустой адрес → error «введите адрес» (пункт C), не «ничего не изменено»."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(url_current="http://old/v1", auth_header="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-url", Input).value = ""
        url, header, error, _proxy = ed.submit()
        assert url is None and error == "введите адрес"


async def test_submit_malformed_url_returns_error():
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(url_current="http://old/v1", auth_header="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-url", Input).value = "notaurl"
        url, header, error, _proxy = ed.submit()
        assert url is None and error == "нужны http(s):// и хост"


async def test_submit_valid_pair():
    """Валидный адрес + токен (галка ○, нет активной авторизации) → (url, header, None)."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://old/v1/chat/completions", auth_header=None
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://new/v1/chat/completions"
        ed.move(1)  # курсор → токен
        await pilot.pause()
        ed.query_one("#conn-token", Input).value = "sk-NEW"
        url, header, error, _proxy = ed.submit()
        assert url == "https://new/v1/chat/completions"
        assert header == "Bearer sk-NEW"
        assert error is None


async def test_bearer_selected_without_token_asks_for_token():
    """Действовала «без авторизации»; галку сняли (Space, выбран Bearer), токен не
    введён → submit просит «введите токен», а не молчит «ничего не изменено»."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://h/v1", auth_header=""
    )  # действует «без авторизации»
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)  # курсор → галка (замороженный токен пропускается при ●)
        assert ed.toggle_no_auth() is True  # ● -> ○ (выбран Bearer)
        url, header, error, _proxy = ed.submit()
        assert error == "введите токен"
        assert url is None and header is None
        assert ed.is_dirty() is True  # незавершённый выбор — секция «изменена»


async def test_bearer_active_empty_field_is_noop():
    """Действует Bearer с токеном; галка не тронута (○), поле пусто → submit ничего
    не меняет (не ошибка): действующий токен остаётся."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://h/v1", auth_header="Bearer sk-OLD"
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)  # курсор → токен, поле пусто
        url, header, error, _proxy = ed.submit()
        assert (url, header, error) == (None, None, None)
        assert ed.is_dirty() is False


async def test_no_auth_toggle_writes_empty_header():
    """Действовал Bearer; галку включили (Space, ●) → submit снимает авторизацию."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://h/v1", auth_header="Bearer sk-OLD"
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)
        ed.move(1)  # курсор → галка
        assert ed.toggle_no_auth() is True  # ○ -> ●
        url, header, error, _proxy = ed.submit()
        assert (url, error) == (None, None)
        assert header == ""


async def test_no_auth_already_active_unchanged_is_noop():
    """Уже действовала «без авторизации» (галка ● по умолчанию, не тронута) →
    submit ничего не пишет (header None), не переписывает "" заново."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(url_current="http://h/v1", auth_header="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        url, header, error, _proxy = ed.submit()
        assert (url, header, error) == (None, None, None)
        assert ed.is_dirty() is False


async def test_is_dirty_false_on_entry():
    """Вход без правок — не dirty (адрес предзаполнен, токен пуст = «не менять»)."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(url_current="http://old/v1", auth_header="Bearer x")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ed.is_dirty() is False


async def test_navigation_past_galka_does_not_toggle():
    """Проход курсором по позициям (без Space) не переключает галку и не делает
    секцию «изменено» — переключение только явным toggle_no_auth()/Space."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://old/v1", auth_header="Bearer x"
    )  # действует Bearer, галка ○
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)
        ed.move(1)
        ed.move(1)  # токен → галка → прокси
        await pilot.pause()
        assert ed.is_dirty() is False
        noauth = str(ed.query_one("#conn-noauth", Static).render())
        assert "○" in noauth and "●" not in noauth  # галка не тронута


async def test_return_to_address_clears_the_checkbox_ghost():
    """Курсор вернулся на адрес: фокус в поле, на галке не осталось призрака подсветки.

    Проверяется продакшен-путём — лентой (`move`), как её водят экраны. Раньше тут
    звали `focus_first()`, но в проде его не вызывал никто: он повторял приземление
    `move` и был удалён (2026-08-07)."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(url_current="http://old/v1", auth_header="Bearer x")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)
        ed.move(1)  # курсор ушёл на галку
        await pilot.pause()
        assert ed.query_one("#conn-noauth", Static).has_class(
            "selected"
        )  # предусловие
        ed.move(-1)
        ed.move(-1)  # и вернулся на адрес
        await pilot.pause()
        assert not ed.query_one("#conn-noauth", Static).has_class("selected")
        assert ed.query_one(
            "#conn-url", Input
        ).has_focus  # фокус вернулся на адрес


async def test_token_field_placeholder_shows_masked_current():
    """Действующий токен виден МАСКОЙ в placeholder поля (поле не выглядит пустым),
    но НЕ заезжает в значение — секретная безопасность сохранена."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://h/v1", auth_header="Bearer sk-SECRETVALUE"
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        tok = ed.query_one("#conn-token", Input)
        assert tok.value == ""  # значение пустое — секрет не заезжает
        assert (
            "SECRETVALUE" not in tok.placeholder
        )  # чувствительная часть под маской
        assert "***" in tok.placeholder  # но поле визуально не пустое


async def test_selected_class_highlights_galka_row():
    """Строка галки получает класс selected, когда курсор на ней (фон-подсветка)."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(url_current="http://h/v1", auth_header="")
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)  # курсор на галке (замороженный токен пропускается при ●)
        await pilot.pause()
        assert ed.query_one("#conn-noauth", Static).has_class("selected")


async def test_toggle_no_auth_off_cursor_is_noop_and_does_not_stop():
    """Space вне позиции галки — toggle_no_auth() возвращает False (хост не стопит клавишу)."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(url_current="http://h/v1", auth_header=None)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ed.toggle_no_auth() is False  # курсор на url (idx 0)
        ed.move(1)  # → token
        assert ed.toggle_no_auth() is False


async def test_toggle_no_auth_freezes_token_input_empty():
    """Space на галке замораживает #conn-token (смок 2026-07-30): поле на месте
    (раскладка не прыгает), но disabled, пустое и без placeholder-хинта."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(url_current="http://h/v1", auth_header=None)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one(
            "#conn-token", Input
        ).value = "typed"  # введённое очищается при ●
        ed.move(1)
        ed.move(1)  # курсор → галка
        assert ed.toggle_no_auth() is True
        await pilot.pause()
        tok = ed.query_one("#conn-token", Input)
        assert tok.display is True  # поле не исчезает
        assert tok.disabled is True
        assert tok.value == "" and tok.placeholder == "не используется"


async def test_toggle_no_auth_twice_restores_token_input():
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(url_current="http://h/v1", auth_header=None)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)
        ed.move(1)
        ed.toggle_no_auth()
        ed.toggle_no_auth()
        await pilot.pause()
        tok = ed.query_one("#conn-token", Input)
        assert tok.disabled is False  # поле ожило
        assert tok.placeholder == "введите токен"  # хинт вернулся


async def test_create_mode_has_no_galka_and_create_hint():
    """create_mode=True (Task 8, онбординг): без галки «без авторизации», позиции
    0=url,1=token,2=proxy; хинт пустого токена — В PLACEHOLDER поля (не отдельной строкой)."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(url_current="", auth_header=None, create_mode=True)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not ed.query(".onb-option")  # ни одной галки/опции
        ph = ed.query_one("#conn-token", Input).placeholder
        assert (
            ph
            == "если не указать — запрос пойдёт без токена (подходит, если сервер его не требует)"
        )
        ed.move(1)
        ed.move(1)  # url -> token -> proxy (3 позиции)
        await pilot.pause()
        assert ed.query_one("#conn-proxy", Input).has_focus


async def test_create_mode_empty_token_submits_without_header():
    """create_mode: пустой токен → header None, никакой ошибки (в отличие от settings)."""
    from gc_tui.editors.connection import ConnectionEditor

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
    from gc_core.config_modules.endpoint_presets import (
        default_endpoint_presets,
    )
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="",
        auth_header=None,
        create_mode=True,
        presets=default_endpoint_presets(),
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        status = str(ed.query_one(".conn-url-preset-hint").render())
        assert (
            status
            == "выберите провайдера или впишите свой адрес · Enter — подставить"
        )


async def test_edit_mode_prefilled_url_shows_canon_preset_hint():
    """Текст строки под адресом — функция состояния списка (канон 2026-08-17).

    Непустой адрес — любой, хоть совпавший с пресетом — список не поднимает, и строка
    называет способ вызвать его вручную. Раскрыли явно (`Alt+↓`) — строка становится
    канонной подсказкой списка."""
    from gc_core.config_modules.endpoint_presets import (
        default_endpoint_presets,
    )
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="https://openrouter.ai/api/v1/chat/completions",
        auth_header=None,
        presets=default_endpoint_presets(),
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert (
            str(ed.query_one(".conn-url-preset-hint").render())
            == "Alt+↓ — показать список"
        )

        ed.reload_presets()  # Alt+↓
        await pilot.pause()
        status = str(ed.query_one(".conn-url-preset-hint").render())
        assert (
            status
            == "провайдеры · печать — фильтр/свой адрес · Enter — подставить · Esc — свернуть"
        )


async def test_no_match_collapses_the_list_and_says_so():
    """Набран свой адрес — в каталоге такого нет: список СВОРАЧИВАЕТСЯ, а строка под
    полем сообщает об этом и называет, чем список вернуть.

    Раньше он оставался раскрытым пустым с хинтом «Enter — оставить свой адрес» —
    выдуманное значение клавиши, которая везде сохраняет форму; заодно первое нажатие
    уходило на сворачивание вместо перехода дальше. И вид зависел от предыстории: с
    пустого поля список висел раскрытым, с заполненного — не поднимался вовсе. Канон
    в редакции 2026-08-18: раскрыт, пока значение складывается И есть из чего
    выбирать."""
    from gc_core.config_modules.endpoint_presets import (
        default_endpoint_presets,
    )
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="", auth_header=None, presets=default_endpoint_presets()
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ed.presets_open  # пустое поле — подходит всё

        ed.query_one(
            "#conn-url", Input
        ).value = "https://my-own-host.internal/v1"
        await pilot.pause()

        assert not ed.presets_open
        assert (
            _hint_under_address(ed)
            == "в списке такого нет · Alt+↓ — показать список"
        )

        # Стрелка — обычная навигация формы, список её не забирает.
        ed.move(1)
        await pilot.pause()
        assert ed.query_one("#conn-token", Input).has_focus
        # Набранное осталось: пресет-лист адреса печать не откатывает
        # (`restore_on_close=False`) — она тут ввод своего значения, а не фильтр.
        assert (
            ed.query_one("#conn-url", Input).value
            == "https://my-own-host.internal/v1"
        )
        # Набранное осталось в поле: пресет-лист адреса печать не откатывает
        # (`restore_on_close=False`) — она тут ввод своего значения, а не фильтр.
        assert (
            ed.query_one("#conn-url", Input).value
            == "https://my-own-host.internal/v1"
        )


async def test_arrow_keeps_walking_the_preset_list_while_it_has_rows():
    """Пока в списке есть что выбирать, стрелка — его: курсор формы остаётся на
    адресе до Enter/Esc (модальность списка, канон §4.2.2)."""
    from gc_core.config_modules.endpoint_presets import (
        default_endpoint_presets,
    )
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="", auth_header=None, presets=default_endpoint_presets()
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)
        await pilot.pause()
        assert ed.presets_open
        assert ed.query_one("#conn-url", Input).has_focus


async def test_create_mode_typed_token_becomes_bearer_header():
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(url_current="", auth_header=None, create_mode=True)
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.query_one("#conn-url", Input).value = "http://h/v1/chat/completions"
        ed.move(1)  # → token
        ed.query_one("#conn-token", Input).value = "sk-NEW"
        url, header, error, _proxy = ed.submit()
        assert error is None
        assert header == "Bearer sk-NEW"


async def test_preset_list_opens_whole_not_filtered_by_the_prefilled_address():
    """Раскрытие поверх заполненного адреса даёт ВЕСЬ список, курсор — на совпавшем
    пресете.

    Живой смок 2026-08-04: в «Подключении» список показывал одну строку, а стоило
    уйти с поля и вернуться — все три. Сужал его `Input.Changed`, который Textual
    постит на поле, смонтированное СО значением, — хозяин честно принимал событие за
    печать.

    ПОСЛЕ канона 2026-08-17 эта ловушка структурно недостижима, и тест её больше НЕ
    сторожит (проверено мутацией на ревью 2026-08-18: верни голое присваивание вместо
    `write_field` — тест пройдёт). Причина: при заполненном адресе список свёрнут, и
    прилетающий `Input.Changed` уходит в `apply_filter`, где это no-op; к моменту
    `Alt+↓` список открывается с пустым фильтром. Тест остаётся проверкой того, что
    форсированное раскрытие даёт ПОЛНЫЙ список с подсветкой совпавшего пресета."""
    from gc_core.config_modules.endpoint_presets import (
        default_endpoint_presets,
    )
    from gc_tui.editors.connection import ConnectionEditor

    presets = default_endpoint_presets()
    ed = ConnectionEditor(
        url_current=presets[1].url, auth_header=None, presets=presets
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ed.query_one("#conn-url", Input).value == presets[1].url
        ed.reload_presets()  # Alt+↓ — раскрыть поверх значения
        await pilot.pause()
        visible = [w for w in ed.query(".conn-preset-row") if w.display]
        assert len(visible) == len(presets), (
            f"список раскрылся суженным: {[str(w.render()) for w in visible]}"
        )
        assert str(visible[1].render()).startswith("▸ "), (
            "▸ не на совпавшем пресете"
        )
        # Печать поверх — уже настоящий фильтр, а не «значение поля».
        ed.query_one("#conn-url", Input).value = presets[0].name.lower()
        await pilot.pause()
        assert len([w for w in ed.query(".conn-preset-row") if w.display]) == 1


async def test_section_entry_shows_the_whole_preset_list(tmp_path):
    """Живой смок 2026-08-04: список раскрывался суженным до одной строки — совпавшего
    пресета, — а стоило уйти с поля и вернуться, как появлялись все. Сужал его
    `Input.Changed` от поля, смонтированного СО значением.

    Через настоящую секцию, а не через контроллер: на минимальном хосте такого события
    нет вовсе. После канона 2026-08-17 вход в секцию список не раскрывает (адрес задан),
    поэтому раскрываем явно.

    Как и у соседа выше, саму ловушку тест больше не ловит — она стала недостижимой
    (мутация на ревью 2026-08-18 проходит). Проверяется полнота списка на боевом пути
    раскрытия секции."""
    from form_harness import settings_connection
    from gc_core.config_modules.endpoint_presets import (
        DEFAULT_ENDPOINT_PRESETS,
    )
    from gc_tui.editors.connection import ConnectionEditor

    async with settings_connection(
        tmp_path, url=DEFAULT_ENDPOINT_PRESETS[1].url
    ) as (pilot, scr):
        ed = scr.query_one(ConnectionEditor)
        ed.reload_presets()  # Alt+↓
        await pilot.pause()
        rows = list(ed.query(".conn-preset-row"))
        visible = [w for w in rows if w.display]
        assert len(visible) == len(DEFAULT_ENDPOINT_PRESETS), (
            f"список раскрылся суженным: {[str(w.render()) for w in visible]}"
        )
        assert str(visible[1].render()).startswith("▸ "), (
            "▸ не на совпавшем пресете"
        )


# --- Ctrl+U: очистка сохранённого токена ---------------------------------------
#
# Токен — единственное поле формы, чьё значение физически не в поле: он секрет,
# поэтому Input держат пустым, а сохранённое показывают маской в placeholder.
# Стереть его обычным Backspace не за что — отсюда отдельное действие.


async def test_clear_token_marks_saved_for_removal():
    """Очистка меняет состояние формы здесь и сейчас: маска уходит, поле
    сообщает, что сохранённый токен будет удалён."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://h/v1/chat/completions", auth_header="Bearer sk-old"
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = ed.query_one("#conn-token", Input)
        assert "***" in inp.placeholder  # до очистки — маска сохранённого

        ed.move(1)  # курсор туда, где обещана клавиша
        assert ed.clear_token() is True
        await pilot.pause()

        assert ed.token_cleared is True
        assert "***" not in inp.placeholder
        assert "удал" in inp.placeholder


async def test_clear_token_is_a_noop_without_saved_token():
    """Стирать нечего — клавиша не наша, пусть её обработает кто-то ещё."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://h/v1/chat/completions", auth_header=None
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ed.clear_token() is False
        assert ed.token_cleared is False


async def test_clear_token_is_a_noop_off_the_token_field():
    """Курсор не на токене — клавиша не наша: футер обещает Ctrl+U ровно на поле
    токена, и на адресе она обязана уйти к Input (там у неё своя роль — стереть
    строку). Иначе сохранённый токен помечается к удалению незаметно."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://h/v1/chat/completions", auth_header="Bearer sk-old"
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert ed.on_token_field_with_saved() is False  # курсор на адресе
        assert ed.clear_token() is False
        assert ed.token_cleared is False


async def test_typing_a_token_cancels_the_clear():
    """Очистил, потом передумал и набрал новый — пометка снимается сама,
    иначе набранное молча не сохранилось бы."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://h/v1/chat/completions", auth_header="Bearer sk-old"
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)
        ed.clear_token()
        ed.query_one("#conn-token", Input).value = "sk-new"
        await pilot.pause()
        assert ed.token_cleared is False


async def test_cleared_token_is_saved_as_empty_header():
    """Сохранение после очистки снимает авторизацию — тот же результат, что у
    галки «без авторизации», но без переключения режима."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://h/v1/chat/completions", auth_header="Bearer sk-old"
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        ed.move(1)
        ed.clear_token()
        _url, header, err, _proxy = ed.submit()
        assert err is None
        assert header == ""


def _hint_under_address(ed) -> str:
    """Текст живой строки-подсказки под полем адреса («» — строки нет)."""
    live = [
        w
        for w in ed.query(Static)
        if "conn-url-preset-hint" in " ".join(w.classes) and not w._pruning
    ]
    return str(live[-1].render()) if live else ""


async def test_hint_stays_when_cursor_leaves_the_address():
    """Строка под адресом постоянна: уход курсора её не снимает и текст не меняет.

    Иначе при ходьбе по форме менялись бы два места сразу — строка и футер, — и глаз
    ловит мельтешение там, где ничего не произошло. `Alt+↓` под свёрнутым списком —
    ярлык способа, а не команда момента, как `Space — переключить` у галки, которая
    тоже работает только со своей позиции. Что нажимать сейчас, отвечает футер."""
    from gc_core.config_modules.endpoint_presets import (
        default_endpoint_presets,
    )
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="https://own-host/v1/chat/completions",
        auth_header="Bearer x",
        presets=default_endpoint_presets(),
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        on_url = _hint_under_address(ed)
        assert "показать список" in on_url

        ed.move(1)  # курсор ушёл на токен
        await pilot.pause()
        assert _hint_under_address(ed) == on_url


async def test_canon_table_of_list_states():
    """Таблица состояний канона 2026-08-17 целиком.

    Врозь эти проверки дают ложное спокойствие: исходный дефект был ровно в том, что
    два состояния из пяти выпадали, а каждое по отдельности выглядело правильным."""
    from gc_core.config_modules.endpoint_presets import (
        default_endpoint_presets,
    )
    from gc_tui.editors.connection import ConnectionEditor

    presets = default_endpoint_presets()

    # 1. пусто → раскрыт, строка — подсказка списка
    ed = ConnectionEditor(
        url_current="", auth_header="Bearer x", presets=presets
    )
    async with _harness(ed).run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert ed._presets_ctrl.is_open
        assert "показать список" not in _hint_under_address(ed)

    # 2-4. пресетный и свой ведут себя ОДИНАКОВО — в этом и суть канона
    for url in (presets[0].url, "https://own-host/v1/chat/completions"):
        ed = ConnectionEditor(
            url_current=url, auth_header="Bearer x", presets=presets
        )
        async with _harness(ed).run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            assert not ed._presets_ctrl.is_open, url
            assert "показать список" in _hint_under_address(ed), url
            ed.move(1)
            await pilot.pause()
            assert ed._positions[ed._idx] == "conn-token", (
                url
            )  # ↓ уводит с адреса

    # 6-7. Alt+↓ поверх значения: своё — без подсветки, пресетное — с подсветкой
    for url, selected in (
        ("https://own-host/v1/chat/completions", False),
        (presets[0].url, True),
    ):
        ed = ConnectionEditor(
            url_current=url, auth_header="Bearer x", presets=presets
        )
        async with _harness(ed).run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            ed.reload_presets()
            await pilot.pause()
            assert ed._presets_ctrl.is_open, url
            assert ed._presets_ctrl.has_selection is selected, url

    # 9. адрес-ссылка ${ENV:...} — непустое значение, ведёт себя как свой адрес
    ed = ConnectionEditor(
        url_current="${ENV:GC_URL}", auth_header="Bearer x", presets=presets
    )
    async with _harness(ed).run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert not ed._presets_ctrl.is_open
        assert "показать список" in _hint_under_address(ed)

    # 10. пустой каталог не раскрывается даже на пустом поле
    ed = ConnectionEditor(url_current="", auth_header="Bearer x", presets=[])
    async with _harness(ed).run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert not ed._presets_ctrl.is_open

    # 11. Esc при заполненном адресе списку не адресован — хозяин обработает сам
    ed = ConnectionEditor(
        url_current=presets[0].url, auth_header="Bearer x", presets=presets
    )
    async with _harness(ed).run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert ed.escape_presets() is False


async def test_hint_survives_pick_and_zero_filter():
    """Два пути закрытия, на которых строка раньше пропадала: Enter (подстановка) и
    ↓ с отфильтрованного в ноль списка. Оба обязаны оставить «Alt+↓ — показать список».

    Именно эти два состояния выпадали из первой редакции правки — их нашло независимое
    ревью, замерив таблицу вместо того, чтобы поверить описанию."""
    from gc_core.config_modules.endpoint_presets import (
        default_endpoint_presets,
    )
    from gc_tui.editors.connection import ConnectionEditor

    presets = default_endpoint_presets()

    ed = ConnectionEditor(
        url_current="", auth_header="Bearer x", presets=presets
    )
    async with _harness(ed).run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert ed._presets_ctrl.is_open  # предусловие: пустое поле раскрыто
        ed.enter_presets()  # Enter — подставить пресет
        await pilot.pause()
        assert not ed._presets_ctrl.is_open
        assert "показать список" in _hint_under_address(ed)

    ed = ConnectionEditor(
        url_current="", auth_header="Bearer x", presets=presets
    )
    async with _harness(ed).run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://nothing-matches-this/v1"
        await pilot.pause()
        ed.move(1)  # фильтр в ноль → ↓ сворачивает
        await pilot.pause()
        assert not ed._presets_ctrl.is_open
        assert "показать список" in _hint_under_address(ed)


async def test_clearing_the_address_reopens_the_list():
    """Стёрли адрес целиком — список раскрывается сам.

    Канон 2026-08-17 — инвариант над ЗНАЧЕНИЕМ поля: пусто ⟹ раскрыт. Первая
    реализация решала это только на входе курсора в поле, и стирание оставляло
    свёрнутый список до ухода-возврата или Alt+↓ (живой смок 2026-08-18). Юнит-тесты
    этого не видели структурно: все конструировали редактор с готовым значением,
    а не редактировали поле."""
    from gc_core.config_modules.endpoint_presets import (
        default_endpoint_presets,
    )
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="https://own-host/v1/chat/completions",
        auth_header="Bearer x",
        presets=default_endpoint_presets(),
    )
    async with _harness(ed).run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert not ed._presets_ctrl.is_open  # предусловие: адрес задан

        ed.query_one("#conn-url", Input).value = ""
        await pilot.pause()

        assert ed._presets_ctrl.is_open
        rows = [w for w in ed.query(".conn-preset-row") if w.display]
        assert len(rows) == len(
            default_endpoint_presets()
        )  # весь список, не сужен


async def test_editing_the_address_reopens_the_list_as_a_filter():
    """Правка заполненного адреса раскрывает список фильтром.

    Канон 2026-08-17 в первой редакции был сформулирован над ЗНАЧЕНИЕМ (пусто →
    раскрыт), и печать поверх заданного адреса список не поднимала. Живой смок
    2026-08-18 показал несогласованность того же рода, ради лечения которой канон и
    затевался: с пустого поля печать фильтрует, с заполненного — нет. Одно действие,
    два поведения, разница только в предыстории.

    Правильная формулировка — над ПРОЦЕССОМ: список раскрыт, пока значение
    складывается (пусто ИЛИ человек прямо сейчас правит поле), и свёрнут, когда оно
    сложилось (вошли с готовым значением и не трогаем)."""
    from gc_core.config_modules.endpoint_presets import (
        default_endpoint_presets,
    )
    from gc_tui.editors.connection import ConnectionEditor

    presets = default_endpoint_presets()
    ed = ConnectionEditor(
        url_current=presets[1].url, auth_header="Bearer x", presets=presets
    )
    async with _harness(ed).run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert not ed._presets_ctrl.is_open  # предусловие: значение сложилось

        # Стёрли последний символ — правка началась.
        ed.query_one("#conn-url", Input).value = presets[1].url[:-1]
        await pilot.pause()

        assert ed._presets_ctrl.is_open
        visible = [w for w in ed.query(".conn-preset-row") if w.display]
        assert len(visible) == 1  # ровно совпавший провайдер
        assert presets[1].name in str(visible[0].render())


async def test_typing_an_unmatched_address_returns_the_arrows_to_the_form():
    """Набран свой адрес — совпадений нет, и раскрытый фильтром список не имеет права
    запереть ↑/↓ на поле: ходить по нему нечем. Стрелка уводит курсор дальше по форме,
    список сворачивается сам (контракт `FieldListController.move`)."""
    from gc_core.config_modules.endpoint_presets import (
        default_endpoint_presets,
    )
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="https://own-host/v1/chat/completions",
        auth_header="Bearer x",
        presets=default_endpoint_presets(),
    )
    async with _harness(ed).run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://my-own-host.internal/v1"
        await pilot.pause()
        assert ed._presets_ctrl.has_matches is False

        ed.move(1)
        await pilot.pause()
        assert ed._positions[ed._idx] == "conn-token"
        assert not ed._presets_ctrl.is_open


# ── probe_snapshot: гейт дребезга видит и то, чего в полях нет ──


async def test_probe_snapshot_follows_the_cleared_token():
    """Ctrl+U меняет то, что уйдёт в запрос (токена не будет), а поля при этом те же:
    снимок обязан отличаться, иначе гейт запретит перепроверку и на экране останется
    вердикт про токен, которого уже нет."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://h/v1/chat/completions", auth_header="Bearer sk-old"
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = ed.probe_snapshot()
        ed.move(1)  # → поле токена
        assert ed.clear_token() is True
        assert ed.probe_snapshot() != before


async def test_probe_snapshot_follows_the_no_auth_toggle():
    """Галка «без авторизации» тоже не поле: она решает, пойдёт ли токен вообще."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://h/v1/chat/completions", auth_header="Bearer sk-old"
    )
    app = _harness(ed)
    async with app.run_test() as pilot:
        await pilot.pause()
        before = ed.probe_snapshot()
        ed.move(2)  # → позиция галки
        assert ed.toggle_no_auth() is True
        assert ed.probe_snapshot() != before


@pytest.mark.asyncio
async def test_a_mouse_click_lands_the_cursor_like_the_arrows_do():
    """Клик мышью по полю — тоже перемещение курсора (спека §4.3): без
    приземления клик не двигал позицию формы — Space на галке и футер судили
    по «прошлому» месту курсора, а список над пустым адресом не раскрывался
    (живой смок 2026-09-04, тот же класс, что у формы записи реестра)."""
    from gc_tui.editors.connection import ConnectionEditor

    ed = ConnectionEditor(
        url_current="http://host/v1/chat/completions", auth_header="Bearer tok"
    )
    async with _harness(ed).run_test() as pilot:
        await pilot.pause()
        assert ed._positions[ed._idx] == "conn-url"  # предусловие
        await pilot.click("#conn-token")
        await pilot.pause()
        assert ed._positions[ed._idx] == "conn-token", (
            "клик не приземлил курсор"
        )
