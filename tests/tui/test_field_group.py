"""Поле со списком — одна рамка на группу (спека §3.1)."""
import pytest


@pytest.mark.asyncio
async def test_address_and_presets_live_in_one_group(tmp_path):
    """Поле и строки списка — потомки одного контейнера с рамкой, а не соседи."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from config_modules.endpoint_presets import default_endpoint_presets
    from textual.widgets import Input
    from textual.containers import VerticalScroll

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        ed = ConnectionEditor(url_current="", auth_header=None, proxy_current="",
                              create_mode=True,
                              presets=default_endpoint_presets())
        await pilot.app.screen.mount(ed)
        await pilot.pause()
        group = ed.query_one("#conn-url-group")
        inp = ed.query_one("#conn-url", Input)
        assert inp.parent is group, "поле должно жить внутри группы"
        scrolls = list(group.query(VerticalScroll))
        assert scrolls, "строки пресетов монтируются внутрь группы, а не рядом"


@pytest.mark.asyncio
async def test_hint_is_below_the_group_not_inside(tmp_path):
    """Подсказка про клавиши — снаружи рамки: внутри неё только выбор (§3.2.2)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from config_modules.endpoint_presets import default_endpoint_presets

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        ed = ConnectionEditor(url_current="", auth_header=None, proxy_current="",
                              create_mode=True,
                              presets=default_endpoint_presets())
        await pilot.app.screen.mount(ed)
        await pilot.pause()
        group = ed.query_one("#conn-url-group")
        hints = [w for w in group.query(".onb-hint")]
        assert not hints, "подсказка не должна быть внутри рамки группы"


@pytest.mark.asyncio
async def test_saved_secret_marked_on_field(tmp_path):
    """Поле с сохранённым токеном несёт маркер — по нему placeholder красится
    как значение, а не как подсказка (спека §3.4)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        ed = ConnectionEditor(url_current="https://h/v1/chat/completions",
                              auth_header="Bearer sk-secret", proxy_current="",
                              create_mode=False, presets=[])
        await pilot.app.screen.mount(ed)
        await pilot.pause()
        assert ed.query_one("#conn-token", Input).has_class("has-saved")

        ed2 = ConnectionEditor(url_current="", auth_header=None, proxy_current="",
                               create_mode=True, presets=[])
        await pilot.app.screen.mount(ed2)
        await pilot.pause()
        assert not ed2.query_one("#conn-token", Input).has_class("has-saved"), \
            "без сохранённого токена маркера быть не должно"


@pytest.mark.asyncio
async def test_proxy_field_has_no_marker(tmp_path):
    """Прокси префиллится значением, а placeholder Textual рисует только у пустого
    поля: маска с маркером видны в единственный момент — когда человек сам стёр
    поле, чтобы прокси СНЯТЬ. Ярко говорить там «сохранённое есть» — против
    намерения, поэтому маркера у прокси нет."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        ed = ConnectionEditor(url_current="https://h/v1/chat/completions",
                              auth_header="Bearer sk-secret",
                              proxy_current="http://proxy:8080",
                              create_mode=False, presets=[])
        await pilot.app.screen.mount(ed)
        await pilot.pause()
        assert not ed.query_one("#conn-proxy", Input).has_class("has-saved")


@pytest.mark.asyncio
async def test_form_hints_are_one_dim_colour(tmp_path):
    """«Тускло» — один токен на всю форму, иначе оппозиция «ярко = сохранённое»
    не читается (спека §3.4). Поле, переехавшее в группу, теряет `set-framed`,
    поэтому базовое правило висит на типе поля, а не на классе."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from tui.palette import DEEP_DIM, INK
    from textual.widgets import Input

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        ed = ConnectionEditor(url_current="https://h/v1/chat/completions",
                              auth_header="Bearer sk-secret", proxy_current="",
                              create_mode=False, presets=[])
        await pilot.app.screen.mount(ed)
        await pilot.pause()

        def colour(wid):
            w = ed.query_one(f"#{wid}", Input)
            return w.get_component_styles("input--placeholder").color.hex.lower()

        # #conn-url живёт в группе (без set-framed), токен и прокси — в рамке поля
        assert colour("conn-url") == DEEP_DIM.lower(), "поле в группе тоже приглушаем сами"
        assert colour("conn-proxy") == DEEP_DIM.lower()
        assert colour("conn-token") == INK.lower(), "маска сохранённого — цветом значения"


@pytest.mark.asyncio
async def test_disabled_field_group_is_dimmed(tmp_path):
    """Поле в группе отключено — приглушается рамка ГРУППЫ (спека §3.3).

    Своей рамки у поля в группе нет (класс `set-framed` оно теряет, иначе рамок
    было бы две), поэтому правило одинокого поля `#conn-token.set-framed:disabled`
    сюда не достаёт, а `:has` Textual 8.2.7 не знает. Признак ставит само поле —
    там же, где меняется `disabled`."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.palette import DEEP_DIM, RUN_COLOR
    from tui.widgets.paste_input import PasteInput
    from textual.containers import Vertical

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        # Поле отключено ещё до монтирования (`PasteInput(disabled=True)` в
        # конструкторе хоста) — рамка обязана быть приглушённой с первого кадра.
        inp = PasteInput(id="conn-token", classes="group-field", disabled=True)
        group = Vertical(inp, id="conn-token-group", classes="field-group")
        await pilot.app.screen.mount(group)
        await pilot.pause()

        def border() -> str:
            return group.styles.border_top[1].hex.lower()

        assert border() == DEEP_DIM.lower(), "замороженное поле — рамка группы гаснет"
        inp.disabled = False
        await pilot.pause()
        assert border() == RUN_COLOR.lower(), "поле ожило — рамка снова рабочая"
        inp.disabled = True
        await pilot.pause()
        assert border() == DEEP_DIM.lower(), "и обратно, тем же движением"


@pytest.mark.asyncio
async def test_marker_drops_when_auth_disabled(tmp_path):
    """● «без авторизации»: placeholder больше не маска — маркера быть не должно."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        ed = ConnectionEditor(url_current="https://h/v1/chat/completions",
                              auth_header="Bearer sk-secret", proxy_current="",
                              create_mode=False, presets=[])
        await pilot.app.screen.mount(ed)
        await pilot.pause()
        assert ed.query_one("#conn-token", Input).has_class("has-saved")
        ed.move(2)          # курсор на строку-галку: toggle работает только там
        assert ed.toggle_no_auth()
        await pilot.pause()
        assert not ed.query_one("#conn-token", Input).has_class("has-saved")


@pytest.mark.asyncio
async def test_alt_down_on_address_opens_the_collapsed_preset_list(tmp_path):
    """Alt+↓ на адресе раскрывает свёрнутый список пресетов.

    Развод клавиш (спека 2026-08-06 §2): раскрытие ушло на Alt+↓ целиком, а
    Ctrl+Space теперь ВСЕГДА про пробу — см. `test_ctrl_space_probes_regardless_of_the_preset_list_state`.
    """
    pytest.importorskip("textual")
    from tests.tui.conftest import settings_connection

    async with settings_connection(tmp_path) as (pilot, scr):
        from tui.editors.connection import ConnectionEditor
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        await pilot.pause()
        assert not ed.presets_open, "предусловие: список свёрнут"
        await pilot.press("alt+down")
        await pilot.pause()
        assert ed.presets_open, "свёрнутый список обязан раскрыться"


@pytest.mark.asyncio
async def test_ctrl_space_probes_regardless_of_the_preset_list_state(tmp_path):
    """Ctrl+Space проверяет связь независимо от состояния пресет-листа (§2.2 спеки
    2026-08-06: клавиши не конкурируют, порядок любой) — список свёрнут, раскрыт
    Alt+↓ явно или так и остаётся раскрытым, проба уходит в обоих случаях."""
    pytest.importorskip("textual")
    from tests.tui.conftest import settings_connection, recording_probe

    calls = []
    async with settings_connection(tmp_path, probe=recording_probe(calls)) as (pilot, scr):
        from tui.editors.connection import ConnectionEditor
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        await pilot.pause()
        await pilot.press("alt+down")            # раскрыли явно
        await pilot.pause()
        assert ed.presets_open, "предусловие: список раскрыт"
        calls.clear()
        await pilot.press("ctrl+space")
        await pilot.pause(0.3)
        assert calls, "проба обязана уйти, список раскрытию не мешает"
        assert ed.presets_open, "проба не имеет права закрыть список"


@pytest.mark.asyncio
async def test_ctrl_down_on_address_opens_the_list_like_alt_down(tmp_path):
    """Ctrl+↓ — тихий синоним Alt+↓ (спека §2): раскрывает свёрнутый пресет-лист
    точно так же, хотя в текстах эта клавиша нигде не называется."""
    pytest.importorskip("textual")
    from tests.tui.conftest import settings_connection

    async with settings_connection(tmp_path) as (pilot, scr):
        from tui.editors.connection import ConnectionEditor
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        await pilot.pause()
        assert not ed.presets_open, "предусловие: список свёрнут"
        await pilot.press("ctrl+down")
        await pilot.pause()
        assert ed.presets_open, "свёрнутый список обязан раскрыться"


@pytest.mark.asyncio
async def test_ctrl_space_on_an_unselected_filter_does_not_probe_the_typed_text(tmp_path):
    """§2.3 спеки 2026-08-06: пока список раскрыт, в поле лежит фильтр (`gr`), а не
    адрес. Ctrl+Space не имеет права стучаться на набранный текст — статус говорит,
    что адрес ещё не выбран, а не шлёт запрос на "gr". Прежний вердикт и снимок
    пробы остаются целы, автопроба не отменяется (иначе связь осталась бы
    непроверенной вовсе, докстринг `_cancel_autoprobe`).

    Сторож нетривиален: печать сама по себе гасит вердикт (`on_input_changed` →
    `_reset_draft_state`, канон, вне скоупа этой задачи) — если снимок вердикта брать
    сразу после печати, `None == None` ничего бы не доказывал (ревью, фикс-раунд 1).
    Чтобы получить ЖИВОЙ вердикт над невалидным полем, уходим с поля стрелкой:
    `_autoprobe_on_leave` пробы не валидирует (её гейт — только у ручного Ctrl+Space,
    брифовое «место проверки — обработчик клавиши») и честно шлёт «gr» в probe_fn,
    вердикт оседает. Возврат на поле той же стрелкой узнаёт те же значения по снимку
    и повторно не пробит — вердикт остаётся ЖИВЫМ, когда мы жмём проверяемую клавишу."""
    pytest.importorskip("textual")
    from textual.widgets import Input, Static
    from tests.tui.conftest import settings_connection, recording_probe, wait_until

    calls = []
    async with settings_connection(tmp_path, probe=recording_probe(calls)) as (pilot, scr):
        from tui.editors.connection import ConnectionEditor
        ed = scr.query_one(ConnectionEditor)
        calls.clear()
        scr.query_one("#conn-url", Input).value = "gr"    # непустой невалидный текст
        await pilot.pause()
        await pilot.press("down")                # уход с поля — автопроба «gr» (вне скоупа §2.3)
        await wait_until(pilot, lambda: scr._draft_checked is not None,
                         "автопроба ухода с поля должна была осесть")
        await pilot.press("up")                  # назад на «gr»: снимок совпал, повторно не пробит
        await pilot.pause()
        calls.clear()
        snapshot = scr._last_probed_snapshot
        checked = scr._draft_checked
        timer = scr._autoprobe_timer
        assert checked is not None, "предусловие: вердикт должен быть живым, не None"
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert not calls, "проба не должна была стартовать"
        probe_line = str(scr.query_one(".set-probe", Static).render())
        assert "адрес" in probe_line
        assert scr._last_probed_snapshot == snapshot, "снимок предыдущей пробы должен уцелеть"
        assert scr._draft_checked is checked, "прежний вердикт трогать нельзя"
        assert scr._autoprobe_timer is timer, "автопроба не должна быть отменена"


@pytest.mark.asyncio
async def test_ctrl_space_on_an_empty_address_falls_back_like_before(tmp_path):
    """§2.3, третье уточнение: правило касается только непустого невалидного текста —
    пустое поле по-прежнему уходит в фолбэк на сохранённый адрес конфига (поведение
    `_start_draft_probe` не меняется)."""
    pytest.importorskip("textual")
    from textual.widgets import Input
    from tests.tui.conftest import settings_connection, recording_probe

    calls = []
    async with settings_connection(tmp_path, probe=recording_probe(calls)) as (pilot, scr):
        calls.clear()
        scr.query_one("#conn-url", Input).value = ""
        await pilot.pause()
        await pilot.press("ctrl+space")
        await pilot.pause(0.3)
        assert calls, "пустое поле обязано уйти в пробу сохранённого адреса, как раньше"


@pytest.mark.asyncio
async def test_ctrl_space_on_an_env_reference_still_probes(tmp_path):
    """Регресс ревью (фикс-раунд 1): `${ENV:VAR}` — штатное СЫРОЕ значение поля
    адреса (config.yml с `completions_url: "${ENV:GC_URL}"`), не фильтр. §2.3 бракует
    её так же, как «gr» (`_valid_endpoint_url` требует http(s)-схему), но блокировать
    пробу здесь нельзя — иначе ручная перепроверка связи для корп-конфигов на
    переменных окружения умирает насовсем. Гейт обязан пропускать ссылку дальше, в
    `_start_draft_probe` — через обработчик клавиши, реальным нажатием."""
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tests.tui.conftest import settings_connection, recording_probe

    calls = []
    async with settings_connection(tmp_path, probe=recording_probe(calls),
                                   url="${ENV:GC_URL}") as (pilot, scr):
        calls.clear()
        await pilot.press("ctrl+space")
        await pilot.pause(0.3)
        assert calls, "ссылка обязана дойти до пробы, а не быть принятой за фильтр"
        probe_line = str(scr.query_one(".set-probe", Static).render())
        assert "сначала выберите адрес" not in probe_line


@pytest.mark.asyncio
async def test_preset_list_opens_full_not_filtered_by_the_current_address(tmp_path):
    """Раскрытый список — ПОЛНЫЙ, а не сужен текущим адресом.

    Было: при первом раскрытии виден один вариант (значение поля прилетало фильтром
    через Input.Changed от начального присвоения), а при повторном — все три. Разное
    поведение одного и того же входа (смок 2026-08-04).

    Канон 2026-08-17: заполненный адрес список сам не поднимает, поэтому раскрываем
    явно (`reload_presets` = Alt+↓). Саму ловушку тест не ловил и раньше (проверено
    мутацией на ревью 2026-08-18) — он сторожит свойство «раскрытый список полон», и
    оно сохранено; добавилась проверка, что заполненный адрес список не поднимает.
    """
    pytest.importorskip("textual")
    from tests.tui.conftest import settings_connection
    from config_modules.endpoint_presets import default_endpoint_presets

    async with settings_connection(tmp_path) as (pilot, scr):
        from tui.editors.connection import ConnectionEditor
        from textual.widgets import Input
        ed = scr.query_one(ConnectionEditor)
        presets = default_endpoint_presets()
        # Сценарий владельца: адрес в конфиге совпадает с пресетом, заходим на секцию.
        ed.query_one("#conn-url", Input).value = presets[0].url
        await pilot.pause()
        ed.escape_presets()
        await pilot.pause()
        # Гоняем лентой, как это делает экран: ушли на соседнее поле и вернулись.
        # Возврат на адрес список больше не поднимает (значение задано) — раскрываем
        # явно, как это делает Alt+↓.
        ed.move(1)
        await pilot.pause()
        ed.move(-1)
        await pilot.pause()
        assert not ed.presets_open, "заполненный адрес список сам не поднимает"
        ed.reload_presets()
        await pilot.pause()
        assert ed.presets_open, "предусловие: Alt+↓ раскрыл список"
        rows = [w for w in ed.query(".conn-preset-row") if w.display]
        assert len(rows) == len(presets), "список обязан быть полным, а не сужен адресом"
