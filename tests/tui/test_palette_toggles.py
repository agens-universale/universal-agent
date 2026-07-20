"""Toggle-строки палитры: состояние справа, ←/→/Enter в открытой палитре
циклят и применяют значение МГНОВЕННО (спека 2026-07-18 §1 — черновик
отменён); Enter текстом (палитра уже закрыта) — только инфо-флеш."""
import pytest


def test_cycle_value_wraps_around():
    """Чистая функция цикла: ±1 по кортежу values, по кругу, без записи."""
    from tui.toggles import cycle_value

    assert cycle_value("gate", "on", 1) == "off"
    assert cycle_value("gate", "off", 1) == "on"
    assert cycle_value("answers", "judge", 1) == "allow"
    assert cycle_value("answers", "allow", 1) == "deny"
    assert cycle_value("answers", "deny", 1) == "judge"
    assert cycle_value("answers", "deny", -1) == "allow"


def test_palette_renders_toggle_state():
    """Строка toggle-команды рисует слайдер и значение справа: `● on`
    (рендер — rich.Text, спека 2026-07-18 §2-4; текст читаем через `.plain`)."""
    pytest.importorskip("textual")
    from tui.commands import Command, CommandRegistry
    from tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("help", (), "", "справка"))
    reg.register(Command("gate", (), "", "финиш-гейт", is_toggle=True))
    palette = CommandPalette(reg, initial_filter="", toggle_state=lambda name: "on")

    gate_row = palette._format_toggle_row(reg.lookup("gate")).plain
    help_row = palette._format_row(reg.lookup("help"))

    assert "●" in gate_row and gate_row.rstrip().endswith("on")
    assert "●" not in help_row


def test_hint_for_no_longer_special_cases_toggle():
    """Task 3: toggle-хинт со значением+подсказкой строит `CommandPalette._toggle_hint`
    (см. tests ниже) — `_hint_for` больше не отличает toggle-команды, `_HINT_TOGGLE`
    удалена как осиротевшая."""
    pytest.importorskip("textual")
    from tui.commands import Command
    from tui.widgets.command_palette import _HINT_NO_ARGS, _hint_for

    toggle_cmd = Command("gate", (), "", "финиш-гейт", is_toggle=True)
    plain_cmd = Command("help", (), "", "справка")

    assert _hint_for(toggle_cmd) == _HINT_NO_ARGS == _hint_for(plain_cmd)


@pytest.mark.asyncio
async def test_palette_footer_hidden_when_no_toggle_commands(tmp_path):
    """Реестр без тогглов — разделителя и toggle-строк нет (подвал сам присутствует
    всегда — в нём живёт hint, см. §порядок ниже)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.commands import Command, CommandRegistry
    from tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("help", (), "", "справка"))

    app = TuiApp(threads_dir=str(tmp_path / "t"), config={"model": "M"})
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(reg, initial_filter="")
        await app.screen.mount(pal)
        await pilot.pause()
        assert not pal.query(".palette-separator")
        assert not pal.query(".palette-toggle-row")


@pytest.mark.asyncio
async def test_palette_footer_shows_docked_toggle_row(tmp_path):
    """Смонтированная палитра с toggle-командой — подвал + разделитель + строка с ●."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.commands import Command, CommandRegistry
    from tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("help", (), "", "справка"))
    reg.register(Command("gate", (), "", "финиш-гейт", is_toggle=True))

    app = TuiApp(threads_dir=str(tmp_path / "t"), config={"model": "M"})
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(reg, initial_filter="", toggle_state=lambda name: "on")
        await app.screen.mount(pal)
        await pilot.pause()

        assert pal.query_one(".palette-footer") is not None
        assert pal.query_one(".palette-separator") is not None
        toggle_rows = list(pal.query(".palette-toggle-row"))
        assert len(toggle_rows) == 1
        row_text = toggle_rows[0].render().plain
        assert "●" in row_text and row_text.rstrip().endswith("on")
        # Верхняя зона (обычные команды) не задета toggle-подвалом.
        assert len(list(pal.query(".palette-row").exclude(".palette-toggle-row"))) == 1


@pytest.mark.asyncio
async def test_palette_footer_vertical_order(tmp_path):
    """Порядок чтения сверху вниз: команды → разделитель → toggle-строки → hint
    (баг: hint жил СНАРУЖИ докнутого подвала и рендерился выше него — dock: bottom
    вырывает из потока только сам контейнер, не соседей по yield-порядку)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.commands import Command, CommandRegistry
    from tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("help", (), "", "справка"))
    reg.register(Command("gate", (), "", "финиш-гейт", is_toggle=True))

    app = TuiApp(threads_dir=str(tmp_path / "t"), config={"model": "M"})
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(reg, initial_filter="", toggle_state=lambda name: "on")
        await app.screen.mount(pal)
        await pilot.pause()

        command_rows = list(pal.query(".palette-row").exclude(".palette-toggle-row"))
        separator = pal.query_one(".palette-separator")
        toggle_rows = list(pal.query(".palette-toggle-row"))
        hint = pal.query_one(".palette-hint")

        assert command_rows[-1].region.y < separator.region.y
        assert separator.region.y < toggle_rows[0].region.y
        assert toggle_rows[-1].region.y < hint.region.y


@pytest.mark.asyncio
async def test_palette_toggle_row_visible_when_filter_empties_top_zone(tmp_path):
    """Геометрический баг: фильтр 'ga' не матчит ни одной обычной команды (верхняя
    зона пуста), но матчит toggle-команду /gate. Докнутый подвал не участвует в
    auto-высоте контейнера .palette — без резервации места контейнер схлопывается
    до одной рамки, и toggle-строка рендерится за пределами региона палитры."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.commands import Command, CommandRegistry
    from tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("help", (), "", "справка"))
    reg.register(Command("gate", (), "", "финиш-гейт", is_toggle=True))

    app = TuiApp(threads_dir=str(tmp_path / "t"), config={"model": "M"})
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(reg, initial_filter="ga", toggle_state=lambda name: "on")
        await app.screen.mount(pal)
        await pilot.pause()

        # Верхняя зона реально пуста.
        assert not list(pal.query(".palette-row").exclude(".palette-toggle-row"))

        toggle_row = pal.query_one(".palette-toggle-row")
        assert toggle_row.region.height > 0
        pal_region = pal.region
        assert toggle_row.region.y >= pal_region.y
        assert toggle_row.region.y + toggle_row.region.height <= pal_region.y + pal_region.height


@pytest.mark.asyncio
async def test_palette_footer_not_hanging_below_border_with_full_list(tmp_path):
    """Геометрический баг: с полным (нефильтрованным) списком команд докнутый
    подвал не резервирует себе места — auto-высота контейнера считается только
    по верхним (не докнутым) строкам, и подвал (разделитель+тоггл-строки+hint)
    рисуется поверх последних строк списка команд вместо того, чтобы идти
    строго ниже них."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.commands import build_default_registry
    from tui.widgets.command_palette import CommandPalette

    reg = build_default_registry()

    app = TuiApp(threads_dir=str(tmp_path / "t"), config={"model": "M"})
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(reg, initial_filter="", toggle_state=lambda name: "on")
        await app.screen.mount(pal)
        await pilot.pause()

        rows = list(pal.query(".palette-row").exclude(".palette-toggle-row"))
        separator = pal.query_one(".palette-separator")
        hint = pal.query_one(".palette-hint")
        pal_region = pal.region

        last_row = rows[-1]
        # Подвал обязан идти СТРОГО ниже последней строки верхней зоны — не
        # перекрывать её.
        assert separator.region.y >= last_row.region.y + last_row.region.height
        # ... и не вылезать за собственную рамку палитры.
        assert hint.region.y + hint.region.height <= pal_region.y + pal_region.height


def test_apply_toggle_writes_config_and_reloads(tmp_path):
    """apply_toggle: пишет в РЕАЛЬНЫЙ config.yml ровно переданное value, reload_config
    вызван (не пересчитывает направление сам — это чинит находку #2 ревью)."""
    pytest.importorskip("textual")
    import yaml
    from tui.app import TuiApp
    from tui.toggles import apply_toggle

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('tui:\n  finish_gate_mode: "on"\n', encoding="utf-8")
    app = TuiApp(config={})
    app._config_path = str(cfg_file)
    app.reload_config()

    calls = {"n": 0}
    orig_reload = app.reload_config

    def counting_reload():
        calls["n"] += 1
        orig_reload()

    app.reload_config = counting_reload

    new_value = apply_toggle(app, "gate", "off")

    assert new_value == "off"
    assert calls["n"] == 1
    data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert data["tui"]["finish_gate_mode"] == "off"


def test_apply_toggle_writes_explicit_value_not_recomputed_direction(tmp_path):
    """Находка #2: приложение обязано писать значение, показанное в драфте, а не
    cycle_value(current, +1) от текущего состояния. current='allow' + draft='deny'
    (юзер крутил ←) — apply_toggle обязан записать 'deny', а не пересчитанный 'judge'."""
    pytest.importorskip("textual")
    import yaml
    from tui.app import TuiApp
    from tui.toggles import apply_toggle

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('tui:\n  text_answer_mode: "allow"\n', encoding="utf-8")
    app = TuiApp(config={})
    app._config_path = str(cfg_file)
    app.reload_config()

    got = apply_toggle(app, "answers", "deny")

    assert got == "deny"
    data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert data["tui"]["text_answer_mode"] == "deny"


def test_apply_toggle_rejects_value_outside_spec(tmp_path):
    """Мусорное/рассинхронизированное value — ValueError, ничего не пишется на диск."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.toggles import apply_toggle

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('tui:\n  finish_gate_mode: "on"\n', encoding="utf-8")
    app = TuiApp(config={})
    app._config_path = str(cfg_file)
    app.reload_config()

    with pytest.raises(ValueError):
        apply_toggle(app, "gate", "maybe")

    assert cfg_file.read_text(encoding="utf-8") == 'tui:\n  finish_gate_mode: "on"\n'


def test_apply_toggle_answers_three_values(tmp_path):
    """Каждое из трёх значений /answers реально применяется и оседает в файле."""
    pytest.importorskip("textual")
    import yaml
    from tui.app import TuiApp
    from tui.toggles import apply_toggle

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('tui:\n  text_answer_mode: "judge"\n', encoding="utf-8")
    app = TuiApp(config={})
    app._config_path = str(cfg_file)
    app.reload_config()

    for value in ("allow", "deny", "judge"):
        got = apply_toggle(app, "answers", value)
        assert got == value
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert data["tui"]["text_answer_mode"] == value


def test_gate_state_shows_effective_not_raw():
    """Глобальный strict=false явный + tui дефолт → провайдер отдаёт 'off' (эффективный)."""
    from tui.toggles import toggle_state

    class _App:
        _config = {
            "tui_finish_gate_mode": "on",
            "tui_finish_gate_mode_explicit": False,
            "strict_turn_finish_explicit_false": True,
        }

    assert toggle_state(_App(), "gate") == "off"


# --- Task 4: роутинг клавиш ←/→/Enter из main.py в палитру ------------------


def _app_with_config(tmp_path, yaml_text):
    """TuiApp с реальным config.yml на диске (тот же паттерн, что apply_toggle-тесты)."""
    from tui.app import TuiApp

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(yaml_text, encoding="utf-8")
    app = TuiApp(config={})
    app._config_path = str(cfg_file)
    app.reload_config()
    return app, cfg_file


@pytest.mark.asyncio
async def test_palette_arrow_applies_toggle_immediately_both_directions(tmp_path):
    """(а) ←/→ на выделенной toggle-строке применяет значение СРАЗУ: пишет
    config.yml обеими сторонами (←=-1, →=+1), рендер сразу показывает
    применённое ●, палитра остаётся открытой."""
    pytest.importorskip("textual")
    import yaml
    app, cfg_file = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/gate"
        await pilot.pause(0.1)
        await pilot.press("left")
        await pilot.pause(0.05)

        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert data["tui"]["finish_gate_mode"] == "off"
        palette = pilot.app.query_one(".palette")
        rows_text = "\n".join(
            r.render().plain for r in palette.query(".palette-toggle-row")
        )
        assert "●" in rows_text and rows_text.rstrip().endswith("off")
        assert pilot.app.query(".palette")  # осталась открыта

        await pilot.press("right")
        await pilot.pause(0.05)

        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert data["tui"]["finish_gate_mode"] == "on"
        rows_text = "\n".join(
            r.render().plain for r in palette.query(".palette-toggle-row")
        )
        assert "●" in rows_text and rows_text.rstrip().endswith("on")


@pytest.mark.asyncio
async def test_palette_enter_cycles_and_applies_keeps_palette_open(tmp_path):
    """(б) Enter на toggle-строке циклит (+1) и применяет мгновенно — тот же
    путь, что ←/→: файл изменился, reload сработал, палитра открыта, текст
    поля НЕ сабмитится."""
    pytest.importorskip("textual")
    import yaml
    app, cfg_file = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/gate"
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.05)

        assert pilot.app.query(".palette")  # палитра осталась открыта
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
        assert data["tui"]["finish_gate_mode"] == "off"
        palette = pilot.app.query_one(".palette")
        rows_text = "\n".join(
            r.render().plain for r in palette.query(".palette-toggle-row")
        )
        assert "●" in rows_text and rows_text.rstrip().endswith("off")
        assert pilot.app.query_one("#cmdinput").text == "/gate"  # текст не сабмитится


@pytest.mark.asyncio
async def test_palette_esc_does_not_touch_config(tmp_path):
    """(в) Esc просто закрывает палитру — отменять нечего (черновика не
    существует), конфиг не тронут."""
    pytest.importorskip("textual")
    app, cfg_file = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/gate"
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.pause(0.05)

        assert not pilot.app.query(".palette")
        assert cfg_file.read_text(encoding="utf-8") == 'tui:\n  finish_gate_mode: "on"\n'


@pytest.mark.asyncio
async def test_palette_arrow_keys_not_intercepted_for_regular_command(tmp_path):
    """(г) ←/→ при выделенной ОБЫЧНОЙ команде — каретка инпута, не палитра."""
    pytest.importorskip("textual")
    app, _ = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/se"  # sessions, settings — обычные команды, тогглы не матчатся
        await pilot.pause(0.1)
        inp.move_cursor(inp.document.end)
        before = inp.cursor_location

        await pilot.press("left")
        await pilot.pause(0.05)

        after = inp.cursor_location
        assert after != before  # каретка сдвинулась — палитра клавишу не забрала
        assert pilot.app.query(".palette")  # палитра осталась открыта как есть


@pytest.mark.asyncio
async def test_typed_gate_command_shows_info_without_changing_value(tmp_path):
    """(д) `/gate` текстом + Enter (без палитры) → НЕ меняет значение, только
    информационный флеш с текущим значением и подсказкой как переключать."""
    pytest.importorskip("textual")
    app, cfg_file = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/gate "  # пробел после имени — arg_options пуст, палитра закрывается сама
        await pilot.pause(0.1)
        assert not pilot.app.query(".palette")

        await pilot.press("enter")
        await pilot.pause(0.05)

        assert cfg_file.read_text(encoding="utf-8") == 'tui:\n  finish_gate_mode: "on"\n'
        footer = pilot.app.query_one("#footer-left")
        footer_text = str(footer.render())
        assert "/gate" in footer_text and "текущее значение: on" in footer_text
        assert "←/→" in footer_text


@pytest.mark.asyncio
async def test_typed_answers_command_shows_info_without_changing_value(tmp_path):
    """То же для `/answers` — паритет с `/gate`."""
    pytest.importorskip("textual")
    app, cfg_file = _app_with_config(tmp_path, 'tui:\n  text_answer_mode: "judge"\n')

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/answers "
        await pilot.pause(0.1)
        assert not pilot.app.query(".palette")

        await pilot.press("enter")
        await pilot.pause(0.05)

        assert cfg_file.read_text(encoding="utf-8") == 'tui:\n  text_answer_mode: "judge"\n'
        footer = pilot.app.query_one("#footer-left")
        footer_text = str(footer.render())
        assert "/answers" in footer_text and "текущее значение: judge" in footer_text
        assert "←/→" in footer_text


@pytest.mark.asyncio
async def test_palette_filter_change_does_not_touch_config(tmp_path):
    """Смена фильтра сама по себе ничего не пишет и не применяет — значение
    тоггла меняется только явным ←/→/Enter на самой строке."""
    pytest.importorskip("textual")
    app, cfg_file = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/gate"
        await pilot.pause(0.1)

        # Сужаем фильтр — команда /gate больше не матчится, toggle-строка исчезает.
        inp.text = "/gatex"
        await pilot.pause(0.05)
        palette = pilot.app.query_one(".palette")
        assert not palette.query(".palette-toggle-row")

        # Расширяем обратно.
        inp.text = "/gate"
        await pilot.pause(0.05)
        assert palette.query(".palette-toggle-row")

        assert cfg_file.read_text(encoding="utf-8") == 'tui:\n  finish_gate_mode: "on"\n'


@pytest.mark.asyncio
async def test_palette_space_on_toggle_row_does_not_cycle(tmp_path):
    """(е) Space на toggle-строке НЕ переключает значение (конфликт с ArgPicker)."""
    pytest.importorskip("textual")
    app, cfg_file = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/gate"
        await pilot.pause(0.1)
        await pilot.press("space")
        await pilot.pause(0.05)

        assert cfg_file.read_text(encoding="utf-8") == 'tui:\n  finish_gate_mode: "on"\n'


# --- Task 2: слайдер-рендер тоггл-строк (спека 2026-07-18 §2-4) -------------


def _reg_with_gate_answers():
    """Реестр с /gate + /answers, описания — генерённые из TOGGLES (та же
    формула, что и в build_default_registry — не дублируем формулировку своей)."""
    pytest.importorskip("textual")
    from tui.commands import Command, CommandRegistry
    from tui.toggles import TOGGLES

    reg = CommandRegistry()
    reg.register(Command("gate", (), "", f"{TOGGLES['gate'].label}: {TOGGLES['gate'].summary}",
                          is_toggle=True))
    reg.register(Command("answers", (), "", f"{TOGGLES['answers'].label}: {TOGGLES['answers'].summary}",
                          is_toggle=True))
    return reg


def _mk_palette(reg, state_map):
    from tui.widgets.command_palette import CommandPalette

    return CommandPalette(reg, initial_filter="", toggle_state=lambda n: state_map[n])


def test_slider_row_plain_text_gate_track():
    """gate (2 значения) → трек `○ ●`/`● ○`, `.plain` заканчивается подписью
    значения; точек-разделителей (`·`) нет — пустые позиции рисуются `○`
    (спека §2-3)."""
    reg = _reg_with_gate_answers()
    pal = _mk_palette(reg, {"gate": "on", "answers": "judge"})

    txt = pal._format_toggle_row(reg.lookup("gate")).plain

    assert txt.rstrip().endswith("○ ●  on")
    assert "·" not in txt


def test_regular_and_toggle_rows_share_name_column():
    """Спека §4: имя-колонка общая для ОБЕИХ зон — длинное имя обычной команды
    сдвигает описание тоггл-строки на ту же общую колонку (находка ревью:
    /gate,/answers обязаны быть в той же колонке, что /help,/sessions)."""
    pytest.importorskip("textual")
    from tui.commands import Command, CommandRegistry
    from tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("verylongcommand", (), "", "обычная команда"))
    reg.register(Command("gate", (), "", "самопроверка выполнения задачи: закрыт ли запрос пользователя",
                          is_toggle=True))
    pal = CommandPalette(reg, initial_filter="", toggle_state=lambda n: "on")

    plain_row = pal._format_row(reg.lookup("verylongcommand"))
    toggle_row = pal._format_toggle_row(reg.lookup("gate")).plain

    assert plain_row.index("обычная") == toggle_row.index("самопроверка")


def test_slider_indicator_and_track_columns_align():
    """Трек gate (3 ячейки) добит пробелами до общего поля (answers,
    5 ячеек), прижат вправо; трек answers не изменился; подписи значений
    начинаются в одной колонке в обеих строках."""
    reg = _reg_with_gate_answers()
    pal = _mk_palette(reg, {"gate": "on", "answers": "judge"})

    g = pal._format_toggle_row(reg.lookup("gate")).plain
    a = pal._format_toggle_row(reg.lookup("answers")).plain

    assert g.rstrip().endswith("○ ●  on") and "·" not in g
    assert "○ ● ○" in a and a.rstrip().endswith("judge")
    assert g.rindex("on") == a.rindex("judge")          # общая колонка подписей
    assert len(g.split("on")[0]) == len(a.split("judge")[0])


def test_slider_colors_semantic():
    """Цвет ползунка/индикатора = цвет значения (spans rich.Text), не позиция."""
    from tui import palette as p

    reg = _reg_with_gate_answers()
    pal = _mk_palette(reg, {"gate": "off", "answers": "judge"})

    text = pal._format_toggle_row(reg.lookup("answers"))
    knob_styles = [str(s.style) for s in text.spans]
    assert any(p.RUN_COLOR.lower() in st.lower() for st in knob_styles)   # judge амбер

    text2 = pal._format_toggle_row(reg.lookup("gate"))
    knob_styles2 = [str(s.style) for s in text2.spans if "bold" in str(s.style).lower()]
    assert any(p.INK.lower() in st.lower() for st in knob_styles2)  # off — белый ползунок

    pal_on = _mk_palette(reg, {"gate": "on", "answers": "judge"})
    text3 = pal_on._format_toggle_row(reg.lookup("gate"))
    assert any(p.ACCENT.lower() in str(s.style).lower() for s in text3.spans)  # on — teal индикатор


@pytest.mark.asyncio
async def test_slider_narrow_terminal_truncation_keeps_common_column(tmp_path):
    """Находка ревью Task 2: `label_w` раньше брался из `spec.names` ТЕКУЩЕЙ
    команды (gate max "off"=3, answers "judge"=5) → head_w/col получались
    per-row, и при усечении узкого терминала треки/подписи разных строк
    съезжали по колонкам (avail=60: gate col=50, answers col=48 — съезд на 2).
    Реальный mount (`app.run_test`) обязателен: у не-смонтированной палитры
    `size.width == 0`, ветка усечения вообще не исполняется."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    reg = _reg_with_gate_answers()
    app = TuiApp(threads_dir=str(tmp_path / "t"), config={"model": "M"})
    async with app.run_test(size=(64, 24)) as pilot:
        pal = _mk_palette(reg, {"gate": "on", "answers": "judge"})
        await app.screen.mount(pal)
        await pilot.pause()

        assert pal.size.width - 2 == 60  # avail из находки ревью

        gate_row = pal._format_toggle_row(reg.lookup("gate")).plain
        answers_row = pal._format_toggle_row(reg.lookup("answers")).plain

        # (1) подпись значения — в одной колонке в обеих строках.
        assert gate_row.rindex("on") == answers_row.rindex("judge")
        # (2) описание усечено многоточием (ветка сужения реально сработала).
        assert "…" in gate_row and "…" in answers_row
        # (3) слайдер+подпись присутствуют целиком, не усечены.
        assert gate_row.rstrip().endswith("○ ●  on")
        assert "○ ● ○" in answers_row and answers_row.rstrip().endswith("judge")


# --- Task 3: hint-строка со значением тоггла + приоритетное усечение (§6) ---


def test_toggle_hint_shows_value_meaning():
    """Выделенная toggle-строка — hint = <значение> — <подсказка из TOGGLES>  ·
    ←/→/Enter — переключить (значение/подсказка всегда от применённого —
    черновика не существует; про Esc в toggle-hint не пишем — спека §6)."""
    reg = _reg_with_gate_answers()
    pal = _mk_palette(reg, {"gate": "on", "answers": "judge"})
    pal._selected = len(pal._visible_rows())              # первая тоггл-строка (gate)

    hint = pal._toggle_hint(reg.lookup("gate"))

    assert hint.startswith("on — самопроверка не даст закончить")
    assert "←/→/Enter — переключить" in hint


def test_toggle_hint_updates_after_state_changes():
    """hint строится от ПРИМЕНЁННОГО значения и меняется вслед за
    toggle_state-провайдером — переключение отражается сразу, без черновика."""
    reg = _reg_with_gate_answers()
    state = {"gate": "on", "answers": "judge"}
    pal = _mk_palette(reg, state)

    hint_on = pal._toggle_hint(reg.lookup("gate"))
    assert hint_on.startswith("on — самопроверка не даст закончить")

    state["gate"] = "off"
    hint_off = pal._toggle_hint(reg.lookup("gate"))
    assert hint_off.startswith("off — самопроверка выключена")


def test_fit_hint_priority_truncation():
    """Порядок жертв §6: 1) хвост подсказки после последнего ' · '; 2) клавиши
    сжимаются до ←/→/Enter; 3) многоточие. Механика видима всегда."""
    from tui.widgets.command_palette import _HINT_KEYS_TOGGLE, _fit_hint
    from tui.toggles import _HINT_KEYS_TOGGLE_SHORT

    value, keys = "on", _HINT_KEYS_TOGGLE
    hint = ("самопроверка не даст закончить, пока запрос не закрыт · "
            "для сложных задач, где важна полнота")

    full = _fit_hint(value, hint, keys, width=200)
    assert "для сложных задач" in full and "переключить" in full

    w80 = _fit_hint(value, hint, keys, width=80)
    assert "для сложных задач" not in w80                  # хвост ушёл первым
    assert "самопроверка" in w80

    w60 = _fit_hint(value, hint, keys, width=60)
    assert _HINT_KEYS_TOGGLE_SHORT in w60                   # клавиши сжались

    w30 = _fit_hint(value, hint, keys, width=30)
    assert w30.endswith("…") or len(w30) <= 30             # механика с многоточием


def test_non_toggle_hint_unchanged():
    """Обычная (не toggle) строка — прежние _HINT_NO_ARGS/_HINT_WITH_ARGS, без
    изменений (toggle-хинт со значением её не задевает)."""
    from tui.commands import Command
    from tui.widgets.command_palette import _HINT_NO_ARGS, _HINT_WITH_ARGS, _hint_for

    plain_cmd = Command("help", (), "", "справка")
    with_args_cmd = Command("view", (), "<path>", "просмотр файла")

    assert _hint_for(plain_cmd) == _HINT_NO_ARGS
    assert _hint_for(with_args_cmd) == _HINT_WITH_ARGS


# --- Task 4: расширяемость (спека §7-8) + carry ревью T3 --------------------


def test_five_value_toggle_works_generically(monkeypatch):
    """Спека §8: фиктивный тоггл на 5 значений — трек/колонки/цикл/hint работают
    без правок кода рендера/палитры (TOGGLES — единственный владелец состава,
    реестр команд конфига не знает)."""
    pytest.importorskip("textual")
    from tui import palette as p
    from tui import toggles as tg
    from tui.commands import Command, CommandRegistry
    from tui.widgets.command_palette import CommandPalette

    five = tg.ToggleSpec(
        dotted="tui.fake_mode",
        values=tuple(
            tg.ToggleValue(f"v{i}", f"подсказка {i} · режим {i}", p.ACCENT) for i in range(5)
        ),
        state=lambda cfg: "v2",
        label="фиктивный тоггл",
        summary="пять значений",
    )
    monkeypatch.setitem(tg.TOGGLES, "fake", five)
    reg = CommandRegistry()
    reg.register(Command("fake", (), "", "пятизначный тоггл", is_toggle=True))
    pal = CommandPalette(reg, toggle_state=lambda n: "v2")

    txt = pal._format_toggle_row(reg.lookup("fake")).plain
    assert "○ ○ ● ○ ○" in txt and txt.rstrip().endswith("v2")
    assert tg.cycle_value("fake", "v4", 1) == "v0"          # цикл по кругу
    hint = pal._toggle_hint(reg.lookup("fake"))
    assert hint.startswith("v2 — подсказка 2")


def test_fit_hint_truncates_only_last_dot_segment_with_multiple_dots():
    """Carry ревью T3: подсказка с НЕСКОЛЬКИМИ ' · ' внутри — усечение хвоста
    режет только ПОСЛЕДНИЙ сегмент (rsplit-семантика §6), а не всё после
    первого разделителя."""
    from tui.widgets.command_palette import _fit_hint

    value, keys = "v2", "Enter — применить · Esc — закрыть"
    hint = "шаг один · шаг два · режим N"

    fitted = _fit_hint(value, hint, keys, width=40)

    assert "режим N" not in fitted
    assert "шаг один · шаг два" in fitted


@pytest.mark.asyncio
async def test_palette_toggle_apply_failure_shows_flash_keeps_palette_and_value(tmp_path, monkeypatch):
    """Находка ревью: apply_toggle кидает (напр. запись на диск невозможна) → флеш
    «✗ не сохранено: …» в футере, палитра остаётся открытой, значение не изменилось
    (config.yml не тронут). Монкипатчим apply_toggle в неймспейсе роутинга main.py —
    интеграция роутинга ←/→/Enter, не файловой записи. Оба маршрута (←/Enter) —
    один и тот же try/except в screens/main.py."""
    pytest.importorskip("textual")
    import tui.screens.main as main_screen

    app, cfg_file = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')

    def _boom(app, name, value):
        raise OSError("disk full")

    monkeypatch.setattr(main_screen, "apply_toggle", _boom)

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/gate"
        await pilot.pause(0.1)

        await pilot.press("left")
        await pilot.pause(0.05)

        footer_text = str(pilot.app.query_one("#footer-left").render())
        assert "не сохранено" in footer_text
        assert pilot.app.query(".palette")
        assert cfg_file.read_text(encoding="utf-8") == 'tui:\n  finish_gate_mode: "on"\n'

        await pilot.press("enter")
        await pilot.pause(0.05)

        footer_text = str(pilot.app.query_one("#footer-left").render())
        assert "не сохранено" in footer_text
        assert pilot.app.query(".palette")
        assert cfg_file.read_text(encoding="utf-8") == 'tui:\n  finish_gate_mode: "on"\n'


@pytest.mark.asyncio
async def test_regular_row_hint_unaffected_by_visible_toggles(tmp_path):
    """Carry ревью T3: e2e на реальном (смонтированном) реестре команд+тогглов —
    выделена ОБЫЧНАЯ строка при видимых toggle-строках в подвале → hint прежний
    (_HINT_NO_ARGS/_HINT_WITH_ARGS), не тоггловый."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.commands import build_default_registry
    from tui.widgets.command_palette import _HINT_NO_ARGS, CommandPalette

    reg = build_default_registry()
    app = TuiApp(threads_dir=str(tmp_path / "t"), config={"model": "M"})
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(reg, initial_filter="", toggle_state=lambda n: "on")
        await app.screen.mount(pal)
        await pilot.pause()

        assert pal.query(".palette-toggle-row")  # тогглы реально видимы в подвале
        assert pal.selected_command().name == "help"  # выделена обычная строка

        hint = pal.query_one(".palette-hint")
        assert hint.render().plain == _HINT_NO_ARGS
