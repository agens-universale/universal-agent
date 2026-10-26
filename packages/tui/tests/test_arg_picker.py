"""ArgPicker integration в TraceScreen: триггер на /export <space>, фильтр, Tab,
Enter (выполняет export), Esc, mutex с командной палитрой."""

import pytest
from gc_core.thread_store import append_new_thread_messages
from gc_testkit.tui_support import tui_settings
from gc_tui.services.stores.file_store import FileTaskStore


def _mk_app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


def test_export_command_has_arg_options():
    from gc_tui.commands import build_default_registry

    export = build_default_registry().lookup("export")
    assert export.arg_options == (
        ("txt", "Вся сессия в читаемом виде"),
        ("json", "Структурированные данные сессии"),
        ("log-txt", "Лог-файл агента, текущий запуск (.log)"),
        ("log-json", "JSONL-файл треда (вся сессия)"),
    )


@pytest.mark.asyncio
async def test_picker_opens_on_export_space(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    async with _mk_app(tmp_path).run_test() as pilot:
        pilot.app.query_one("#cmdinput").text = "/export "
        await pilot.pause(0.1)
        assert len(pilot.app.query(ArgPicker)) == 1
        rows = "\n".join(
            str(s.render())
            for s in pilot.app.query_one(ArgPicker).query(".palette-row")
        )
        assert "txt" in rows and "json" in rows


@pytest.mark.asyncio
async def test_picker_filters_by_prefix(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    async with _mk_app(tmp_path).run_test() as pilot:
        pilot.app.query_one("#cmdinput").text = "/export j"
        await pilot.pause(0.1)
        rows = "\n".join(
            str(s.render())
            for s in pilot.app.query_one(ArgPicker).query(".palette-row")
        )
        assert "json" in rows
        assert "txt" not in rows


@pytest.mark.asyncio
async def test_tab_completes_value_and_closes(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    async with _mk_app(tmp_path).run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/export j"
        await pilot.pause(0.1)
        await pilot.press("tab")
        await pilot.pause(0.1)
        assert inp.text == "/export json"
        assert len(pilot.app.query(ArgPicker)) == 0


@pytest.mark.asyncio
async def test_enter_runs_export_with_selected_value(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    append_new_thread_messages(
        str(tmp_path),
        "cli",
        "tidX",
        [{"role": "user", "content": "q", "id": "m1"}],
        set(),
    )
    from gc_tui.widgets.arg_picker import ArgPicker

    async with _mk_app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "tidX"
        scr._store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/export json"
        await pilot.pause(0.1)
        assert len(pilot.app.query(ArgPicker)) == 1
        await pilot.press("enter")
        await pilot.pause(0.1)
        files = list(tmp_path.glob("export-tidX-*.json"))
        assert len(files) == 1


@pytest.mark.asyncio
async def test_esc_closes_keeps_input(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    async with _mk_app(tmp_path).run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/export j"
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert len(pilot.app.query(ArgPicker)) == 0
        assert inp.text == "/export j"


@pytest.mark.asyncio
async def test_mutex_with_command_palette(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker
    from gc_tui.widgets.command_palette import CommandPalette

    async with _mk_app(tmp_path).run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/exp"
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 1
        assert len(pilot.app.query(ArgPicker)) == 0

        inp.text = "/export "
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 0
        assert len(pilot.app.query(ArgPicker)) == 1

        inp.text = "/export"
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 1
        assert len(pilot.app.query(ArgPicker)) == 0


@pytest.mark.asyncio
async def test_hint_has_keys_margin_and_honest_desired_height(tmp_path):
    """Hint-строка пикера — докнутый footer (.palette-footer) с воздухом
    (.palette-keys margin-top) + desired_height честно включает эту строку margin
    (иначе main.py недосчитывал бы высоту и нижняя рамка уезжала за экран —
    класс бага palette desired_height)."""
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    async with _mk_app(tmp_path).run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/export "
        await pilot.pause(0.1)
        picker = pilot.app.query_one(ArgPicker)
        hint = picker.query(".palette-keys")
        assert len(hint) == 1
        assert picker.query_one(".palette-footer").query(
            ".palette-keys"
        )  # hint внутри докнутого подвала
        assert picker.desired_height == len(picker.children) + 3
        # рамка обнимает контент: регион hint внутри региона пикера
        assert hint[0].region.bottom <= picker.region.bottom


@pytest.mark.asyncio
async def test_hint_visible_on_open_with_small_screen_no_scroll(tmp_path):
    """Регрессия c0622c9a на маленьком экране: /export с 4 опциями на 16-строчном
    экране упирается в потолок overlay (main.py::_sync_chrome_caps режет
    picker.region до 4 строк — 2 border + 2 контента) — hint видна СРАЗУ при
    открытии, без скролла (докнутый .palette-footer, а не последний FLOW-ребёнок)."""
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    async with _mk_app(tmp_path).run_test(size=(80, 16)) as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/export "
        await pilot.pause(0.1)
        picker = pilot.app.query_one(ArgPicker)
        assert (
            picker.region.height < picker.desired_height
        )  # клэмп реально сработал
        hint = picker.query(".palette-keys")
        assert len(hint) == 1
        assert hint[0].region.bottom <= 16  # на экране
        assert (
            hint[0].region.bottom <= picker.region.bottom
        )  # внутри рамки пикера, видна без скролла


_LONG_REASON = (
    "C:/Users/dmitr/.gc/profiles/broken-enc.yml: не удалось прочитать: "
    "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
)


async def _picker_with_hint(pilot, hint: str):
    """Пикер с ДИНАМИЧЕСКИМ хинтом, открытый настоящим экраном (настоящие стили:
    .palette-row nowrap, .palette-hint — переносится)."""
    from gc_tui.widgets.arg_picker import ArgPicker

    pilot.app.screen._open_arg_picker(
        options=[
            ("без профиля", "работать чистым конфигом", ""),
            ("alpha", "глобальный", ""),
            ("broken-enc", "глобальный", "✗ не применится"),
        ],
        prefix="",
        hint_provider=lambda v: hint,
    )
    await pilot.pause()
    await pilot.pause()
    return pilot.app.query_one(ArgPicker)


@pytest.mark.parametrize("width", (120, 100, 80, 60))
@pytest.mark.asyncio
async def test_desired_height_covers_a_wrapping_hint(tmp_path, width):
    """`desired_height` — БЮДЖЕТ: min-height пикера, остаток экрана берёт
    трейс. Занизив его, пикер рисуется выше выданного места: подвал накрывает
    хвост списка, а нижняя рамка уезжает за край.

    У `.palette-row` намеренно `text-wrap: nowrap`, у `.palette-hint` — нет, и
    причина отказа (путь + текст исключения, 130+ символов) переносится на 2-3
    строки уже при 80 колонках. Резерв «ровно одна строка» врал на каждой из них."""
    pytest.importorskip("textual")
    async with _mk_app(tmp_path).run_test(size=(width, 40)) as pilot:
        picker = await _picker_with_hint(pilot, _LONG_REASON)
        assert picker.desired_height >= picker.outer_size.height, (
            f"ширина {width}: заявлено {picker.desired_height}, нарисовано {picker.outer_size.height}"
        )


@pytest.mark.asyncio
async def test_wrapping_hint_keeps_the_whole_reason_on_screen(tmp_path):
    """Резерв растёт под причину, а не режет её: путь профиля рядом с палитрой не
    печатает больше никто — без него человеку нечего чинить."""
    pytest.importorskip("textual")
    async with _mk_app(tmp_path).run_test(size=(80, 40)) as pilot:
        picker = await _picker_with_hint(pilot, _LONG_REASON)
        hint = picker.query(".palette-keys")[0]
        assert hint.region.height > 1  # причина правда переносится
        assert (
            hint.region.bottom <= picker.region.bottom
        )  # и целиком внутри рамки
        assert str(hint.render()).startswith(
            "C:/Users/dmitr/.gc/profiles/broken-enc.yml"
        )


@pytest.mark.asyncio
async def test_short_hint_still_reserves_one_line(tmp_path):
    """Короткий хинт не должен дорожать: резерв считается по ТЕКСТУ, а не «на всякий
    случай максимумом» — иначе список без нужды теряет строки."""
    pytest.importorskip("textual")
    async with _mk_app(tmp_path).run_test(size=(80, 40)) as pilot:
        picker = await _picker_with_hint(pilot, "default gc · глобальный")
        # 3 строки + рамка(2) + клавиши (margin + строка) + хинт (margin + строка)
        assert picker.desired_height == 3 + 2 + 2 + 2
        assert picker.desired_height >= picker.outer_size.height


def _model_like_options():
    from gc_tui import palette as pal
    from gc_tui.widgets.arg_picker import PickerOption
    from rich.text import Text

    return [
        PickerOption(
            "auto",
            "модель выбирается автоматически по ходу работы",
            lead=Text("●", style=pal.BRAND),
            enabled=False,
        ),
        PickerOption("", "", separator=True),
        PickerOption("glm-flash", ""),
        PickerOption("giga-max", "", "активна"),
        PickerOption("", "", separator=True),
        PickerOption("qwen-coder", ""),
    ]


def test_separator_skipped_by_navigation_and_filter():
    """Черта не выбирается: стрелки перешагивают её; фильтр оставляет черту,
    только если по обе стороны есть видимые строки."""
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    p = ArgPicker(_model_like_options())
    seen = [p.selected_value()]
    for _ in range(4):
        p.move_selection(1)
        seen.append(p.selected_value())
    # старт — первая доступная строка, auto недоступен
    assert seen == ["glm-flash", "giga-max", "qwen-coder", "auto", "glm-flash"]
    p.move_selection(-1)
    assert p.selected_value() == "auto"

    p.set_filter("g")
    assert [o.value for o in p._visible_rows()] == ["glm-flash", "giga-max"]
    p.set_filter("q")
    assert [o.value for o in p._visible_rows()] == ["qwen-coder"]
    p.set_filter("")
    assert [o.separator for o in p._visible_rows()] == [
        False,
        True,
        False,
        False,
        True,
        False,
    ]


def test_disabled_row_is_dim_and_not_taken():
    """`enabled=False` — строка видна приглушённой, выбор её не берёт."""
    pytest.importorskip("textual")
    from gc_tui import palette as pal
    from gc_tui.widgets.arg_picker import ArgPicker

    p = ArgPicker(_model_like_options(), initial_filter="a")
    assert p.selected_value() == "auto"
    assert p.selected_disabled()
    row = p._format_row(p._visible_rows()[0])
    # Знак сохраняет цвет, приглушены только имя и описание.
    dot = row.plain.index("●")
    styled = {(s.start, s.end): str(s.style) for s in row.spans}
    assert styled[(dot, dot + 1)] == pal.BRAND
    assert styled[(dot + 2, len(row.plain))] == pal.DIM
    p.set_filter("")
    assert p.selected_value() == "glm-flash"
    assert not p.selected_disabled()
    p.set_filter("zzz")  # нет видимых строк — Enter не блокируется
    assert not p.selected_disabled()


@pytest.mark.asyncio
async def test_old_two_and_three_tuples_still_work(tmp_path, monkeypatch):
    """Старые 2-кортежи (/export) и 3-кортежи (/profile) нормализуются в
    PickerOption: значения, описания и статус на месте."""
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker, PickerOption

    p = ArgPicker([("txt", "вся сессия"), ("json", "данные")])
    assert p._visible_rows() == [
        PickerOption("txt", "вся сессия"),
        PickerOption("json", "данные"),
    ]
    assert p._format_row(p._visible_rows()[0]) == " txt    вся сессия"
    p3 = ArgPicker([("smoke", "глобальный", "активен")])
    assert p3._visible_rows()[0].status == "активен"
    assert p3._format_row(p3._visible_rows()[0]).plain == (
        " smoke   активен   глобальный"
    )

    monkeypatch.chdir(tmp_path)
    async with _mk_app(tmp_path).run_test() as pilot:
        pilot.app.query_one("#cmdinput").text = "/profile "
        await pilot.pause(0.1)
        rows = pilot.app.query_one(ArgPicker)._visible_rows()
        assert rows and all(isinstance(r, PickerOption) for r in rows)
        assert rows[0].value == "без профиля"


@pytest.mark.asyncio
async def test_enter_on_disabled_row_keeps_picker_open(tmp_path, monkeypatch):
    """Enter на недоступной строке ничего не выполняет: пикер остаётся."""
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    monkeypatch.chdir(tmp_path)
    async with _mk_app(tmp_path).run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/model "
        await pilot.pause(0.1)
        picker = pilot.app.query_one(ArgPicker)
        assert picker.selected_value() == "auto"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert len(pilot.app.query(ArgPicker)) == 1
        assert "model" not in pilot.app._session_overrides


def _registry_app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "models": {"glm-4.6": {}, "giga-max": {}},
                "slots": {"default": "glm-4.6", "max": "giga-max"},
            }
        ),
    )


async def _type_and_enter(pilot, text):
    inp = pilot.app.query_one("#cmdinput")
    inp.text = text
    await pilot.pause(0.1)
    await pilot.press("enter")
    await pilot.pause(0.1)


@pytest.mark.asyncio
async def test_enter_with_unknown_typed_model_reaches_the_refusal(
    tmp_path, monkeypatch
):
    """Фильтр не оставил строк — Enter уходит с набранным аргументом, и
    команда отвечает своим отказом."""
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    monkeypatch.chdir(tmp_path)
    async with _registry_app(tmp_path).run_test() as pilot:
        hints = []
        pilot.app.screen._show_footer_refusal = hints.append
        await _type_and_enter(pilot, "/model giga-mx")
        assert len(pilot.app.query(ArgPicker)) == 0
        assert hints == ["✗ нет такой модели: giga-mx"]
        assert "model" not in pilot.app._session_overrides


@pytest.mark.asyncio
async def test_enter_with_typed_level_word_applies_it(tmp_path, monkeypatch):
    """Слово уровня — допустимый выбор, хоть строки в пикере у него нет."""
    pytest.importorskip("textual")

    monkeypatch.chdir(tmp_path)
    async with _registry_app(tmp_path).run_test() as pilot:
        await _type_and_enter(pilot, "/model light")
        assert pilot.app._session_overrides.get("model") == "light"


@pytest.mark.asyncio
async def test_enter_with_unknown_profile_reaches_its_refusal(
    tmp_path, monkeypatch
):
    pytest.importorskip("textual")

    monkeypatch.chdir(tmp_path)
    async with _registry_app(tmp_path).run_test() as pilot:
        hints = []
        pilot.app.screen._show_footer_refusal = hints.append
        await _type_and_enter(pilot, "/profile nope")
        assert hints and 'профиль "nope" не найден' in hints[0]


def test_picker_opens_on_the_current_value_when_it_is_selectable():
    """`initial_value` — стартовая подсветка на текущем значении; невидимое
    или недоступное значение — первая доступная строка."""
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    opts = _model_like_options()
    assert ArgPicker(opts, initial_value="giga-max").selected_value() == (
        "giga-max"
    )
    assert ArgPicker(opts, initial_value="auto").selected_value() == (
        "glm-flash"
    )
    assert ArgPicker(opts, initial_value="nope").selected_value() == (
        "glm-flash"
    )
    filtered = ArgPicker(opts, initial_filter="q", initial_value="giga-max")
    assert filtered.selected_value() == "qwen-coder"


def test_picker_without_keys_line_drops_it_and_its_height():
    """`keys_hint=None` — строки клавиш нет, и высота её не резервирует."""
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    full = ArgPicker(_model_like_options())
    bare = ArgPicker(_model_like_options(), keys_hint=None)
    assert full.desired_height - bare.desired_height == 2

    def footer_texts(picker):
        footer = picker._build_footer()
        return [str(w.render()) for w in footer._pending_children]

    assert any("Tab" in t for t in footer_texts(full))
    assert not any("Tab" in t for t in footer_texts(bare))


@pytest.mark.asyncio
async def test_model_picker_opens_on_the_current_choice(tmp_path, monkeypatch):
    """`/model ` открывает список на действующем выборе: Enter сразу его не
    меняет."""
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    monkeypatch.chdir(tmp_path)
    app = _registry_app(tmp_path)
    app.apply_model_choice("max")
    async with app.run_test() as pilot:
        pilot.app.query_one("#cmdinput").text = "/model "
        await pilot.pause(0.1)
        assert pilot.app.query_one(ArgPicker).selected_value() == "giga-max"


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["/model ", "/profile "])
async def test_choice_pickers_say_enter_selects(tmp_path, monkeypatch, typed):
    """`/model` и `/profile` только выбирают значение — строка клавиш
    говорит «Enter выбрать»; `/export` запускает действие — «выполнить»."""
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    monkeypatch.chdir(tmp_path)
    async with _registry_app(tmp_path).run_test() as pilot:
        pilot.app.query_one("#cmdinput").text = typed
        await pilot.pause(0.1)
        picker = pilot.app.query_one(ArgPicker)
        keys = [str(w.render()) for w in picker.query(".palette-keys")]
        assert keys[-1] == (
            "↑↓ выбрать · Tab дополнить · Enter выбрать · Esc закрыть"
        )


@pytest.mark.asyncio
async def test_action_picker_keeps_enter_runs(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    monkeypatch.chdir(tmp_path)
    async with _registry_app(tmp_path).run_test() as pilot:
        pilot.app.query_one("#cmdinput").text = "/export "
        await pilot.pause(0.1)
        picker = pilot.app.query_one(ArgPicker)
        keys = [str(w.render()) for w in picker.query(".palette-keys")]
        assert "Enter выполнить" in keys[-1]


_HINTS = {
    "short": "default gc · глобальный",
    "long": _LONG_REASON,
    "mid": "giga.example.ru:8443",
}


async def _picker_with_hints(pilot, hints: dict, prefix: str = ""):
    """Пикер, у строк которого подсказки разной длины."""
    from gc_tui.widgets.arg_picker import ArgPicker

    pilot.app.screen._open_arg_picker(
        options=[(value, "описание") for value in hints],
        prefix=prefix,
        hint_provider=lambda v: hints.get(v, ""),
    )
    await pilot.pause()
    await pilot.pause()
    return pilot.app.query_one(ArgPicker)


def _hint_widget(picker):
    return picker.query(".palette-keys")[0]


@pytest.mark.asyncio
async def test_picker_height_does_not_jump_on_selection(tmp_path):
    """Место под подсказку — по самой длинной подсказке видимых строк, а не
    по выбранной: ход стрелкой не меняет ни высоту пикера, ни высоту
    подсказки (смок, раунд 2, п. 8)."""
    pytest.importorskip("textual")
    async with _mk_app(tmp_path).run_test(size=(80, 40)) as pilot:
        picker = await _picker_with_hints(pilot, _HINTS)
        assert picker.selected_value() == "short"
        heights = set()
        for _ in _HINTS:
            heights.add(
                (
                    picker.desired_height,
                    picker.outer_size.height,
                    _hint_widget(picker).region.height,
                )
            )
            picker.move_selection(1)
            await pilot.pause()
        assert len(heights) == 1, heights
        assert _hint_widget(picker).region.height > 1  # резерв — под длинную


@pytest.mark.asyncio
async def test_picker_hint_reserve_follows_the_filter(tmp_path):
    """Резерв считается по ВИДИМЫМ строкам: фильтр убрал строку с длинной
    подсказкой — место под неё больше не держится."""
    pytest.importorskip("textual")
    async with _mk_app(tmp_path).run_test(size=(80, 40)) as pilot:
        picker = await _picker_with_hints(pilot, _HINTS)
        assert _hint_widget(picker).region.height > 1
        picker.set_filter("s")
        await pilot.pause()
        assert _hint_widget(picker).region.height == 1
        assert picker.desired_height == picker.outer_size.height


@pytest.mark.parametrize("size", ((60, 30), (100, 24)))
@pytest.mark.asyncio
async def test_long_list_stays_capped_and_scrolls_to_selection(tmp_path, size):
    """≈40 строк на узком экране: пикер не выше своего потолка (у оверлея
    ввода — половина экрана) и прокручивается — выбранная строка видна над
    подвалом и при переходе в конец списка, и обратно в начало."""
    pytest.importorskip("textual")
    hints = {f"model-{i:02d}": f"host-{i}.example.ru" for i in range(40)}
    async with _mk_app(tmp_path).run_test(size=size) as pilot:
        picker = await _picker_with_hints(pilot, hints)
        screen_h = pilot.app.size.height
        assert picker.outer_size.height <= screen_h // 2
        for step in (-1, 1):  # заворот в конец, затем обратно в начало
            picker.move_selection(step)
            await pilot.pause()
            await pilot.pause()
            row = picker.query_one(".palette-row.selected")
            footer = picker.query_one(".palette-footer")
            assert row.region.height == 1, picker.selected_value()
            assert row.region.y > picker.region.y
            assert row.region.bottom <= footer.region.y


@pytest.mark.asyncio
async def test_long_list_opens_scrolled_to_the_current_value(tmp_path):
    """Текущее значение в хвосте длинного списка видно сразу при открытии:
    пикер докручивает к стартовой подсветке без хода стрелкой."""
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker

    async with _mk_app(tmp_path).run_test(size=(60, 30)) as pilot:
        pilot.app.screen._open_arg_picker(
            options=[(f"model-{i:02d}", "описание") for i in range(40)],
            prefix="",
            hint_provider=lambda v: v or "",
            initial_value="model-35",
        )
        await pilot.pause()
        await pilot.pause()
        picker = pilot.app.query_one(ArgPicker)
        row = picker.query_one(".palette-row.selected")
        footer = picker.query_one(".palette-footer")
        assert picker.selected_value() == "model-35"
        assert row.region.height == 1
        assert row.region.bottom <= footer.region.y
