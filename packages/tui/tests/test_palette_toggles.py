"""Toggle-строки палитры: состояние справа, ←/→/Enter в открытой палитре
циклят и применяют значение МГНОВЕННО (спека 2026-07-18 §1 — черновик
отменён); Enter текстом (палитра уже закрыта) — только инфо-флеш."""

from gc_testkit.tui_support import tui_settings
import pytest


def test_cycle_value_wraps_around():
    """Чистая функция цикла: ±1 по кортежу values, по кругу, без записи."""
    from gc_tui.toggles import cycle_value

    assert cycle_value("gate", "on", 1) == "off"  # (idx2+1)%3 -> off
    assert (
        cycle_value("gate", "off", 1) == "smart"
    )  # было "on" -> теперь "smart"
    assert cycle_value("gate", "smart", 1) == "on"
    assert cycle_value("answers", "judge", 1) == "deny"
    assert cycle_value("answers", "allow", 1) == "judge"
    assert cycle_value("answers", "deny", 1) == "allow"
    assert cycle_value("answers", "deny", -1) == "judge"


def test_palette_renders_toggle_state(monkeypatch):
    """Строка toggle-команды рисует слайдер и значение справа: `● on`
    (рендер — rich.Text, спека 2026-07-18 §2-4; текст читаем через `.plain`)."""
    pytest.importorskip("textual")
    from dataclasses import replace

    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.toggles import TOGGLES
    from gc_tui.widgets.command_palette import CommandPalette

    # Механика рендера, не видимость (Task 4 §4.1) — gate виден в пустом фильтре.
    monkeypatch.setitem(
        TOGGLES, "gate", replace(TOGGLES["gate"], palette_default=True)
    )

    reg = CommandRegistry()
    reg.register(Command("help", (), "", "справка"))
    reg.register(Command("gate", (), "", "финиш-гейт", is_toggle=True))
    palette = CommandPalette(
        reg, initial_filter="", toggle_state=lambda name: "on"
    )

    gate_row = palette._format_toggle_row(reg.lookup("gate")).plain
    help_row = palette._format_row(reg.lookup("help"))

    assert "●" in gate_row and gate_row.rstrip().endswith("on")
    assert "●" not in help_row


def test_hint_for_no_longer_special_cases_toggle():
    """Task 3: toggle-хинт со значением+подсказкой строит `CommandPalette._toggle_hint`
    (см. tests ниже) — `_hint_for` больше не отличает toggle-команды, `_HINT_TOGGLE`
    удалена как осиротевшая."""
    pytest.importorskip("textual")
    from gc_tui.commands import Command
    from gc_tui.widgets.command_palette import _HINT_NO_ARGS, _hint_for

    toggle_cmd = Command("gate", (), "", "финиш-гейт", is_toggle=True)
    plain_cmd = Command("help", (), "", "справка")

    assert _hint_for(toggle_cmd) == _HINT_NO_ARGS == _hint_for(plain_cmd)


@pytest.mark.asyncio
async def test_palette_footer_hidden_when_no_toggle_commands(tmp_path):
    """Реестр без тогглов — разделителя и toggle-строк нет (подвал сам присутствует
    всегда — в нём живёт hint, см. §порядок ниже)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("help", (), "", "справка"))

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
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
    from gc_tui.app import TuiApp
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("help", (), "", "справка"))
    reg.register(Command("rigor", (), "", "строгость: пресет", is_toggle=True))

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(
            reg, initial_filter="", toggle_state=lambda name: "medium"
        )
        await app.screen.mount(pal)
        await pilot.pause()

        assert pal.query_one(".palette-footer") is not None
        assert pal.query_one(".palette-separator") is not None
        toggle_rows = list(pal.query(".palette-toggle-row"))
        assert len(toggle_rows) == 1
        row_text = toggle_rows[0].render().plain
        assert "●" in row_text and row_text.rstrip().endswith("medium")
        # Верхняя зона (обычные команды) не задета toggle-подвалом.
        assert (
            len(list(pal.query(".palette-row").exclude(".palette-toggle-row")))
            == 1
        )


@pytest.mark.asyncio
async def test_palette_footer_vertical_order(tmp_path):
    """Порядок чтения сверху вниз: команды → разделитель → toggle-строки → hint
    (баг: hint жил СНАРУЖИ докнутого подвала и рендерился выше него — dock: bottom
    вырывает из потока только сам контейнер, не соседей по yield-порядку)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("help", (), "", "справка"))
    reg.register(Command("rigor", (), "", "строгость: пресет", is_toggle=True))

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(
            reg, initial_filter="", toggle_state=lambda name: "medium"
        )
        await app.screen.mount(pal)
        await pilot.pause()

        command_rows = list(
            pal.query(".palette-row").exclude(".palette-toggle-row")
        )
        separator = pal.query_one(".palette-separator")
        toggle_rows = list(pal.query(".palette-toggle-row"))
        hint = pal.query_one(".palette-hint")

        assert command_rows[-1].region.y < separator.region.y
        assert separator.region.y < toggle_rows[0].region.y
        assert toggle_rows[-1].region.y < hint.region.y


@pytest.mark.asyncio
async def test_palette_toggle_row_visible_when_filter_empties_top_zone(
    tmp_path,
):
    """Геометрический баг: фильтр 'ga' не матчит ни одной обычной команды (верхняя
    зона пуста), но матчит toggle-команду /gate. Докнутый подвал не участвует в
    auto-высоте контейнера .palette — без резервации места контейнер схлопывается
    до одной рамки, и toggle-строка рендерится за пределами региона палитры."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("help", (), "", "справка"))
    reg.register(Command("gate", (), "", "финиш-гейт", is_toggle=True))

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(
            reg, initial_filter="ga", toggle_state=lambda name: "on"
        )
        await app.screen.mount(pal)
        await pilot.pause()

        # Верхняя зона реально пуста.
        assert not list(
            pal.query(".palette-row").exclude(".palette-toggle-row")
        )

        toggle_row = pal.query_one(".palette-toggle-row")
        assert toggle_row.region.height > 0
        pal_region = pal.region
        assert toggle_row.region.y >= pal_region.y
        assert (
            toggle_row.region.y + toggle_row.region.height
            <= pal_region.y + pal_region.height
        )


@pytest.mark.asyncio
async def test_palette_footer_not_hanging_below_border_with_full_list(
    tmp_path,
):
    """Геометрический баг: с полным (нефильтрованным) списком команд докнутый
    подвал не резервирует себе места — auto-высота контейнера считается только
    по верхним (не докнутым) строкам, и подвал (разделитель+тоггл-строки+hint)
    рисуется поверх последних строк списка команд вместо того, чтобы идти
    строго ниже них."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.commands import build_default_registry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = build_default_registry()

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(
            reg, initial_filter="", toggle_state=lambda name: "on"
        )
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
        assert (
            hint.region.y + hint.region.height
            <= pal_region.y + pal_region.height
        )


def test_apply_toggle_writes_session_and_invalidates_runner(tmp_path):
    """apply_toggle: НЕ пишет в config.yml — переопределяет текущую сессию,
    сохраняет в метаданные треда и зовёт invalidate_runner (не reload_config;
    спека 2026-07-20-session-scoped-settings §2)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.toggles import apply_toggle

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('tui:\n  finish_gate_mode: "on"\n', encoding="utf-8")
    app = TuiApp(settings=tui_settings({}))
    app._config_path = str(cfg_file)
    app.reload_config()
    app._threads_dir = str(
        tmp_path / "threads"
    )  # изолируем метаданные от реального gc-state
    app.tui_state.active_task_id = "t1"

    calls = {"n": 0}
    orig_invalidate = app.invalidate_runner

    def counting_invalidate():
        calls["n"] += 1
        orig_invalidate()

    app.invalidate_runner = counting_invalidate

    new_value = apply_toggle(app, "gate", "off")

    assert new_value == "off"
    assert calls["n"] == 1
    assert (
        cfg_file.read_text(encoding="utf-8")
        == 'tui:\n  finish_gate_mode: "on"\n'
    )

    from gc_tui.services.stores.file_store import FileTaskStore

    store = FileTaskStore(threads_dir=app._threads_dir, system=app._system)
    saved = (store.get_task_meta("t1").get("tui") or {}).get(
        "session_settings"
    )
    assert saved == {"tui.finish_gate_mode": "off"}


def test_apply_toggle_writes_explicit_value_not_recomputed_direction(tmp_path):
    """Находка #2: приложение обязано применить значение, показанное в драфте, а не
    cycle_value(current, +1) от текущего состояния. current='allow' + draft='deny'
    (юзер крутил ←) — apply_toggle обязан выставить 'deny', а не пересчитанный 'judge'."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.toggles import apply_toggle, toggle_state

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        'tui:\n  text_answer_mode: "allow"\n', encoding="utf-8"
    )
    app = TuiApp(settings=tui_settings({}))
    app._config_path = str(cfg_file)
    app.reload_config()

    got = apply_toggle(app, "answers", "deny")

    assert got == "deny"
    assert toggle_state(app, "answers") == "deny"
    assert (
        cfg_file.read_text(encoding="utf-8")
        == 'tui:\n  text_answer_mode: "allow"\n'
    )


def test_apply_toggle_rejects_value_outside_spec(tmp_path):
    """Мусорное/рассинхронизированное value — ValueError, ничего не пишется на диск."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.toggles import apply_toggle

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('tui:\n  finish_gate_mode: "on"\n', encoding="utf-8")
    app = TuiApp(settings=tui_settings({}))
    app._config_path = str(cfg_file)
    app.reload_config()

    with pytest.raises(ValueError):
        apply_toggle(app, "gate", "maybe")

    assert (
        cfg_file.read_text(encoding="utf-8")
        == 'tui:\n  finish_gate_mode: "on"\n'
    )


def test_apply_toggle_answers_three_values(tmp_path):
    """Каждое из трёх значений /answers реально применяется — в сессии, файл не трогается."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.toggles import apply_toggle, toggle_state

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        'tui:\n  text_answer_mode: "judge"\n', encoding="utf-8"
    )
    app = TuiApp(settings=tui_settings({}))
    app._config_path = str(cfg_file)
    app.reload_config()

    for value in ("allow", "deny", "judge"):
        got = apply_toggle(app, "answers", value)
        assert got == value
        assert toggle_state(app, "answers") == value
        assert (
            cfg_file.read_text(encoding="utf-8")
            == 'tui:\n  text_answer_mode: "judge"\n'
        )


def test_gate_state_shows_effective_not_raw():
    """Глобальный strict=false явный + tui дефолт → провайдер отдаёт 'off' (эффективный)."""
    from gc_tui.toggles import toggle_state

    from gc_testkit.settings import make_settings

    class _App:
        def effective_settings(self):
            return make_settings(
                **{"agent.strict_turn_finish_validation": False}
            )

    assert toggle_state(_App(), "gate") == "off"


# --- Task 4: роутинг клавиш ←/→/Enter из main.py в палитру ------------------


def _app_with_config(tmp_path, yaml_text):
    """TuiApp с реальным config.yml на диске (тот же паттерн, что apply_toggle-тесты)."""
    from gc_tui.app import TuiApp

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(yaml_text, encoding="utf-8")
    app = TuiApp(settings=tui_settings({}))
    app._config_path = str(cfg_file)
    app.reload_config()
    return app, cfg_file


@pytest.mark.asyncio
async def test_palette_arrow_applies_toggle_immediately_both_directions(
    tmp_path,
):
    """(а) ←/→ на выделенной toggle-строке применяет значение СРАЗУ: переопределяет
    сессию обеими сторонами (←=-1, →=+1), рендер сразу показывает применённое ●,
    config.yml не трогается, палитра остаётся открытой."""
    pytest.importorskip("textual")
    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "on"\n'
    )
    original = cfg_file.read_text(encoding="utf-8")

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/gate"
        await pilot.pause(0.1)
        await pilot.press("left")
        await pilot.pause(0.05)

        assert cfg_file.read_text(encoding="utf-8") == original
        palette = pilot.app.query_one(".palette")
        rows_text = "\n".join(
            r.render().plain for r in palette.query(".palette-toggle-row")
        )
        assert "●" in rows_text and rows_text.rstrip().endswith(
            "smart"
        )  # было "off"
        assert pilot.app.query(".palette")  # осталась открыта

        await pilot.press("right")
        await pilot.pause(0.05)

        assert cfg_file.read_text(encoding="utf-8") == original
        rows_text = "\n".join(
            r.render().plain for r in palette.query(".palette-toggle-row")
        )
        assert "●" in rows_text and rows_text.rstrip().endswith("on")


@pytest.mark.asyncio
async def test_palette_enter_cycles_and_applies_keeps_palette_open(tmp_path):
    """(б) Enter на toggle-строке циклит (+1) и применяет мгновенно — тот же
    путь, что ←/→: сессия изменилась, config.yml не тронут, палитра открыта,
    текст поля НЕ сабмитится."""
    pytest.importorskip("textual")
    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "on"\n'
    )
    original = cfg_file.read_text(encoding="utf-8")

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/gate"
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.05)

        assert pilot.app.query(".palette")  # палитра осталась открыта
        assert cfg_file.read_text(encoding="utf-8") == original
        palette = pilot.app.query_one(".palette")
        rows_text = "\n".join(
            r.render().plain for r in palette.query(".palette-toggle-row")
        )
        assert "●" in rows_text and rows_text.rstrip().endswith("off")
        assert (
            pilot.app.query_one("#cmdinput").text == "/gate"
        )  # текст не сабмитится


@pytest.mark.asyncio
async def test_palette_esc_does_not_touch_config(tmp_path):
    """(в) Esc просто закрывает палитру — отменять нечего (черновика не
    существует), конфиг не тронут."""
    pytest.importorskip("textual")
    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "on"\n'
    )

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/gate"
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.pause(0.05)

        assert not pilot.app.query(".palette")
        assert (
            cfg_file.read_text(encoding="utf-8")
            == 'tui:\n  finish_gate_mode: "on"\n'
        )


@pytest.mark.asyncio
async def test_palette_arrow_keys_not_intercepted_for_regular_command(
    tmp_path,
):
    """(г) ←/→ при выделенной ОБЫЧНОЙ команде — каретка инпута, не палитра."""
    pytest.importorskip("textual")
    app, _ = _app_with_config(tmp_path, 'tui:\n  finish_gate_mode: "on"\n')

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = (
            "/se"  # sessions, settings — обычные команды, тогглы не матчатся
        )
        await pilot.pause(0.1)
        inp.move_cursor(inp.document.end)
        before = inp.cursor_location

        await pilot.press("left")
        await pilot.pause(0.05)

        after = inp.cursor_location
        assert (
            after != before
        )  # каретка сдвинулась — палитра клавишу не забрала
        assert pilot.app.query(".palette")  # палитра осталась открыта как есть


@pytest.mark.asyncio
async def test_typed_gate_command_shows_info_without_changing_value(tmp_path):
    """(д) `/gate` текстом + Enter (без палитры) → НЕ меняет значение, только
    информационный флеш с текущим значением и подсказкой как переключать."""
    pytest.importorskip("textual")
    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "on"\n'
    )

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/gate "  # пробел после имени — arg_options пуст, палитра закрывается сама
        await pilot.pause(0.1)
        assert not pilot.app.query(".palette")

        await pilot.press("enter")
        await pilot.pause(0.05)

        assert (
            cfg_file.read_text(encoding="utf-8")
            == 'tui:\n  finish_gate_mode: "on"\n'
        )
        footer = pilot.app.query_one("#footer-left")
        footer_text = str(footer.render())
        assert "/gate" in footer_text and "текущее значение: on" in footer_text
        assert "←/→" in footer_text


@pytest.mark.asyncio
async def test_typed_answers_command_shows_info_without_changing_value(
    tmp_path,
):
    """То же для `/answers` — паритет с `/gate`."""
    pytest.importorskip("textual")
    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  text_answer_mode: "judge"\n'
    )

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/answers "
        await pilot.pause(0.1)
        assert not pilot.app.query(".palette")

        await pilot.press("enter")
        await pilot.pause(0.05)

        assert (
            cfg_file.read_text(encoding="utf-8")
            == 'tui:\n  text_answer_mode: "judge"\n'
        )
        footer = pilot.app.query_one("#footer-left")
        footer_text = str(footer.render())
        assert (
            "/answers" in footer_text
            and "текущее значение: judge" in footer_text
        )
        assert "←/→" in footer_text


@pytest.mark.asyncio
async def test_typed_rigor_command_shows_info_without_changing_value(tmp_path):
    """То же для `/rigor` — паритет с `/gate`/`/answers` (пресетный тоггл)."""
    pytest.importorskip("textual")
    app, cfg_file = _app_with_config(
        tmp_path,
        'tui:\n  finish_gate_mode: "on"\n  text_answer_mode: "deny"\n',
    )
    original = cfg_file.read_text(encoding="utf-8")

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/rigor "
        await pilot.pause(0.1)
        assert not pilot.app.query(".palette")

        await pilot.press("enter")
        await pilot.pause(0.05)

        assert cfg_file.read_text(encoding="utf-8") == original
        footer = pilot.app.query_one("#footer-left")
        footer_text = str(footer.render())
        assert (
            "/rigor" in footer_text and "текущее значение: high" in footer_text
        )
        assert "←/→" in footer_text


@pytest.mark.asyncio
async def test_palette_filter_change_does_not_touch_config(tmp_path):
    """Смена фильтра сама по себе ничего не пишет и не применяет — значение
    тоггла меняется только явным ←/→/Enter на самой строке."""
    pytest.importorskip("textual")
    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "on"\n'
    )

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

        assert (
            cfg_file.read_text(encoding="utf-8")
            == 'tui:\n  finish_gate_mode: "on"\n'
        )


@pytest.mark.asyncio
async def test_palette_space_on_toggle_row_does_not_cycle(tmp_path):
    """(е) Space на toggle-строке НЕ переключает значение (конфликт с ArgPicker)."""
    pytest.importorskip("textual")
    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "on"\n'
    )

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/gate"
        await pilot.pause(0.1)
        await pilot.press("space")
        await pilot.pause(0.05)

        assert (
            cfg_file.read_text(encoding="utf-8")
            == 'tui:\n  finish_gate_mode: "on"\n'
        )


# --- Task 2: слайдер-рендер тоггл-строк (спека 2026-07-18 §2-4) -------------


def _reg_with_gate_answers(monkeypatch):
    """Реестр с /gate + /answers, описания — генерённые из TOGGLES (та же
    формула, что и в build_default_registry — не дублируем формулировку своей).
    Эти тесты проверяют механику рендера слайдера, а не видимость (Task 4
    §4.1) — делаем оба тоггла видимыми в пустом фильтре монкипатчем
    `palette_default`, иначе `_visible_toggle_rows()` пуст (gate/answers
    default-скрыты) и `_toggle_columns()` падает на пустом max()."""
    pytest.importorskip("textual")
    from dataclasses import replace

    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.toggles import TOGGLES

    monkeypatch.setitem(
        TOGGLES, "gate", replace(TOGGLES["gate"], palette_default=True)
    )
    monkeypatch.setitem(
        TOGGLES, "answers", replace(TOGGLES["answers"], palette_default=True)
    )

    reg = CommandRegistry()
    reg.register(
        Command(
            "gate",
            (),
            "",
            f"{TOGGLES['gate'].label}: {TOGGLES['gate'].summary}",
            is_toggle=True,
        )
    )
    reg.register(
        Command(
            "answers",
            (),
            "",
            f"{TOGGLES['answers'].label}: {TOGGLES['answers'].summary}",
            is_toggle=True,
        )
    )
    return reg


def _mk_palette(reg, state_map):
    from gc_tui.widgets.command_palette import CommandPalette

    return CommandPalette(
        reg, initial_filter="", toggle_state=lambda n: state_map[n]
    )


def test_slider_row_plain_text_gate_track(monkeypatch):
    """gate (2 значения) → трек `○ ●`/`● ○`, `.plain` заканчивается подписью
    значения; точек-разделителей (`·`) нет — пустые позиции рисуются `○`
    (спека §2-3)."""
    reg = _reg_with_gate_answers(monkeypatch)
    pal = _mk_palette(reg, {"gate": "on", "answers": "judge"})

    txt = pal._format_toggle_row(reg.lookup("gate")).plain

    assert txt.rstrip().endswith("○ ○ ●  on")  # было "○ ●  on"
    assert "·" not in txt


def test_regular_and_toggle_rows_share_name_column(monkeypatch):
    """Спека §4: имя-колонка общая для ОБЕИХ зон — длинное имя обычной команды
    сдвигает описание тоггл-строки на ту же общую колонку (находка ревью:
    /gate,/answers обязаны быть в той же колонке, что /help,/sessions)."""
    pytest.importorskip("textual")
    from dataclasses import replace

    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.toggles import TOGGLES
    from gc_tui.widgets.command_palette import CommandPalette

    # Механика колонок, не видимость (Task 4 §4.1) — gate виден в пустом фильтре.
    monkeypatch.setitem(
        TOGGLES, "gate", replace(TOGGLES["gate"], palette_default=True)
    )

    reg = CommandRegistry()
    reg.register(Command("verylongcommand", (), "", "обычная команда"))
    reg.register(
        Command(
            "gate",
            (),
            "",
            "самопроверка выполнения задачи: закрыт ли запрос пользователя",
            is_toggle=True,
        )
    )
    pal = CommandPalette(reg, initial_filter="", toggle_state=lambda n: "on")

    plain_row = pal._format_row(reg.lookup("verylongcommand"))
    toggle_row = pal._format_toggle_row(reg.lookup("gate")).plain

    assert plain_row.index("обычная") == toggle_row.index("самопроверка")


def test_slider_indicator_and_track_columns_align(monkeypatch):
    """Трек gate (3 ячейки) добит пробелами до общего поля (answers,
    5 ячеек), прижат вправо; трек answers не изменился; подписи значений
    начинаются в одной колонке в обеих строках."""
    reg = _reg_with_gate_answers(monkeypatch)
    pal = _mk_palette(reg, {"gate": "on", "answers": "judge"})

    g = pal._format_toggle_row(reg.lookup("gate")).plain
    a = pal._format_toggle_row(reg.lookup("answers")).plain

    assert g.rstrip().endswith("○ ○ ●  on") and "·" not in g  # было "○ ●  on"
    assert "○ ● ○" in a and a.rstrip().endswith("judge")
    assert g.rindex("on") == a.rindex("judge")  # общая колонка подписей
    assert len(g.split("on")[0]) == len(a.split("judge")[0])


def test_child_rows_always_dim_flat_and_track_aligned_with_parent():
    """Ревизия 7 (спека §2, §6): дети видны ВСЕГДА под родителем, без условия
    выделения (композиция статична — ничего не нужно выставлять на
    `_selected`, в отличие от ревизии 5/6) — flat (никакого отступа), имя+
    описание СТИЛЕМ DIM, подпись ЗНАЧЕНИЯ ребёнка тоже DIM. Выравнивание
    трека — по ЛЕВОМУ краю общего поля: базовый трек ВСЕХ строк начинается в
    ОДНОЙ колонке (`col`), custom-резерв — правее троек, паддинг у детей
    достраивается СПРАВА."""
    pytest.importorskip("textual")
    from gc_tui import palette as p
    from gc_tui.commands import build_default_registry
    from gc_tui.toggles import TOGGLES
    from gc_tui.widgets.command_palette import CommandPalette

    reg = build_default_registry()
    state = {"rigor": "high", "gate": "on", "answers": "deny"}

    def _extra(name):
        return False if TOGGLES[name].preset_keys else None

    pal = CommandPalette(
        reg,
        initial_filter="",
        toggle_state=lambda n: state[n],
        toggle_extra=_extra,
    )

    col, base_w, extra_w, label_w = pal._toggle_columns()
    assert (
        base_w == 5 and extra_w == 3
    )  # тройки(5) + custom-резерв(3) — общие для всех строк

    rigor_row = pal._format_toggle_row(reg.lookup("rigor"))
    gate_row = pal._format_toggle_row(reg.lookup("gate"))
    answers_row = pal._format_toggle_row(reg.lookup("answers"))
    rigor_plain, gate_plain, answers_plain = (
        rigor_row.plain,
        gate_row.plain,
        answers_row.plain,
    )

    # Flat (ревизия 7) — никакого отступа ни у кого, включая детей (было +2
    # пробела у ребёнка в ревизии 5/6).
    assert gate_plain.startswith(" /gate") and not gate_plain.startswith(
        "  /gate"
    )
    assert answers_plain.startswith(
        " /answers"
    ) and not answers_plain.startswith("  /answers")
    assert rigor_plain.startswith(" /rigor") and not rigor_plain.startswith(
        "  /rigor"
    )

    # Левый край: базовый трек всех трёх строк начинается РОВНО в колонке `col`.
    assert rigor_plain[col] in ("●", "○")
    assert gate_plain[col] in ("●", "○")
    assert answers_plain[col] in ("●", "○")

    # Общая колонка подписей значений — контракт не изменился, паддинг ушёл
    # с левого края трека на правый (после троек/резерва).
    assert (
        rigor_plain.rindex("high")
        == gate_plain.rindex("on")
        == answers_plain.rindex("deny")
    )

    # Имя+описание ребёнка — стилем DIM (весь head, включая слеш).
    dim_spans_gate = [
        s for s in gate_row.spans if p.DIM.lower() in str(s.style).lower()
    ]
    assert dim_spans_gate and dim_spans_gate[0].start == 0
    assert dim_spans_gate[0].end >= len(" /gate")
    dim_spans_answers = [
        s for s in answers_row.spans if p.DIM.lower() in str(s.style).lower()
    ]
    assert dim_spans_answers and dim_spans_answers[0].start == 0

    # Подпись значения ребёнка (§4.1, новое) — тоже DIM, отдельным span'ом
    # у хвоста строки (трек между head и подписью — семантические цвета,
    # не задет).
    gate_value_start = gate_plain.rindex("on")
    assert any(
        p.DIM.lower() in str(s.style).lower()
        and s.start <= gate_value_start < s.end
        for s in gate_row.spans
    )
    answers_value_start = answers_plain.rindex("deny")
    assert any(
        p.DIM.lower() in str(s.style).lower()
        and s.start <= answers_value_start < s.end
        for s in answers_row.spans
    )

    # Родитель /rigor НЕ дымится DIM-ом (обычный toggle-стиль строки).
    assert not any(
        p.DIM.lower() in str(s.style).lower() for s in rigor_row.spans
    )


def test_filtered_dedups_child_matching_filter(monkeypatch):
    """Ревизия 7 (спека §2, дедуп — правило не изменилось, только без условия
    выделения): непустой фильтр, совпадающий И с родителем, И с ребёнком
    (реальных имён с общим префиксом нет — тогглы синтетические, generic-
    механика не завязана на конкретные имена) — ребёнок обязан попасть в
    подвал ОДИН раз, на позиции ребёнка сразу после родителя (оформлен как
    ребёнок), а не дважды (один раз сам по фильтру, второй раз родительской
    вставкой)."""
    pytest.importorskip("textual")
    from gc_tui import palette as p
    from gc_tui import toggles as tg
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    child = tg.ToggleSpec(
        dotted="tui.zz_child",
        values=(
            tg.ToggleValue("a", "hint a", p.INK),
            tg.ToggleValue("b", "hint b", p.ACCENT),
        ),
        state=lambda cfg: "a",
        label="дочерний фиктивный тоггл",
        summary="демо",
        palette_default=False,
    )
    parent = tg.ToggleSpec(
        values=(
            tg.ToggleValue("x", "hint x", p.INK),
            tg.ToggleValue("y", "hint y", p.ACCENT),
        ),
        state=lambda cfg: "x",
        label="родительский фиктивный тоггл",
        summary="демо",
        preset_keys=("tui.zz_child",),
        palette_default=True,
    )
    monkeypatch.setitem(tg.TOGGLES, "zzchild", child)
    monkeypatch.setitem(tg.TOGGLES, "zzparent", parent)

    reg = CommandRegistry()
    reg.register(Command("zzparent", (), "", "родитель", is_toggle=True))
    reg.register(Command("zzchild", (), "", "ребёнок", is_toggle=True))
    state = {"zzparent": "x", "zzchild": "a"}
    pal = CommandPalette(reg, toggle_state=lambda n: state[n])

    pal.set_filter("zz")  # startswith -> матчит ОБА: zzparent И zzchild

    names = [c.name for c in pal._visible_toggle_rows()]
    assert names == ["zzparent", "zzchild"]  # один экземпляр, не два
    assert (
        pal._owning_preset("zzchild") == "zzparent"
    )  # оформлен как ребёнок группы
    child_row = pal._format_toggle_row(reg.lookup("zzchild"))
    assert child_row.plain.startswith(
        " /zzchild"
    ) and not child_row.plain.startswith("  /zzchild")


def test_filtered_dedups_child_matching_filter_registered_before_parent(
    monkeypatch,
):
    """Находка ревью Z1: дедуп был порядко-зависимым — `injected` помечался
    только когда ребёнка вставлял родитель, а если Command ребёнка
    зарегистрирован в реестре РАНЬШЕ родителя и оба матчат фильтр, ребёнок
    сначала попадал в `result` напрямую из базы (ещё не помечен), а потом ещё
    раз — родительской вставкой (дубликат: `['zzchild','zzparent','zzchild']`).
    Недостижимо сегодня (`/rigor` регистрируется раньше `/gate`,`/answers`),
    но ревизия 7 явно рассчитана на будущие независимые группы — состав
    подвала обязан быть дедуплицирован НЕЗАВИСИМО от порядка регистрации."""
    pytest.importorskip("textual")
    from gc_tui import palette as p
    from gc_tui import toggles as tg
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    child = tg.ToggleSpec(
        dotted="tui.zz_child",
        values=(
            tg.ToggleValue("a", "hint a", p.INK),
            tg.ToggleValue("b", "hint b", p.ACCENT),
        ),
        state=lambda cfg: "a",
        label="дочерний фиктивный тоггл",
        summary="демо",
        palette_default=False,
    )
    parent = tg.ToggleSpec(
        values=(
            tg.ToggleValue("x", "hint x", p.INK),
            tg.ToggleValue("y", "hint y", p.ACCENT),
        ),
        state=lambda cfg: "x",
        label="родительский фиктивный тоггл",
        summary="демо",
        preset_keys=("tui.zz_child",),
        palette_default=True,
    )
    monkeypatch.setitem(tg.TOGGLES, "zzchild", child)
    monkeypatch.setitem(tg.TOGGLES, "zzparent", parent)

    reg = CommandRegistry()
    reg.register(
        Command("zzchild", (), "", "ребёнок", is_toggle=True)
    )  # ребёнок ПЕРВЫМ
    reg.register(
        Command("zzparent", (), "", "родитель", is_toggle=True)
    )  # родитель ВТОРЫМ
    state = {"zzparent": "x", "zzchild": "a"}
    pal = CommandPalette(reg, toggle_state=lambda n: state[n])

    pal.set_filter("zz")  # startswith -> матчит ОБА: zzchild И zzparent

    names = [c.name for c in pal._visible_toggle_rows()]
    assert names == [
        "zzparent",
        "zzchild",
    ]  # один экземпляр, СРАЗУ после родителя


def test_slider_colors_semantic(monkeypatch):
    """Цвет ползунка/индикатора = цвет значения (spans rich.Text), не позиция."""
    from gc_tui import palette as p

    reg = _reg_with_gate_answers(monkeypatch)
    pal = _mk_palette(reg, {"gate": "off", "answers": "judge"})

    text = pal._format_toggle_row(reg.lookup("answers"))
    knob_styles = [str(s.style) for s in text.spans]
    assert any(
        p.RUN_COLOR.lower() in st.lower() for st in knob_styles
    )  # judge амбер

    text2 = pal._format_toggle_row(reg.lookup("gate"))
    knob_styles2 = [
        str(s.style) for s in text2.spans if "bold" in str(s.style).lower()
    ]
    assert any(
        p.INK.lower() in st.lower() for st in knob_styles2
    )  # off — белый ползунок

    pal_on = _mk_palette(reg, {"gate": "on", "answers": "judge"})
    text3 = pal_on._format_toggle_row(reg.lookup("gate"))
    assert any(
        p.ACCENT.lower() in str(s.style).lower() for s in text3.spans
    )  # on — teal индикатор


@pytest.mark.asyncio
async def test_slider_narrow_terminal_truncation_keeps_common_column(
    tmp_path, monkeypatch
):
    """Находка ревью Task 2: `label_w` раньше брался из `spec.names` ТЕКУЩЕЙ
    команды (gate max "off"=3, answers "judge"=5) → head_w/col получались
    per-row, и при усечении узкого терминала треки/подписи разных строк
    съезжали по колонкам (avail=60: gate col=50, answers col=48 — съезд на 2).
    Реальный mount (`app.run_test`) обязателен: у не-смонтированной палитры
    `size.width == 0`, ветка усечения вообще не исполняется."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    reg = _reg_with_gate_answers(monkeypatch)
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
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
        assert gate_row.rstrip().endswith("○ ○ ●  on")  # было "○ ●  on"
        assert "○ ● ○" in answers_row and answers_row.rstrip().endswith(
            "judge"
        )


# --- Task 3: hint-строка со значением тоггла + приоритетное усечение (§6) ---


def test_toggle_hint_shows_value_meaning(monkeypatch):
    """Выделенная toggle-строка — hint = <значение> — <подсказка из TOGGLES>  ·
    ←/→/Enter — переключить (значение/подсказка всегда от применённого —
    черновика не существует; про Esc в toggle-hint не пишем — спека §6)."""
    reg = _reg_with_gate_answers(monkeypatch)
    pal = _mk_palette(reg, {"gate": "on", "answers": "judge"})
    pal._selected = len(pal._visible_rows())  # первая тоггл-строка (gate)

    hint = pal._toggle_hint(reg.lookup("gate"))

    assert hint.startswith("on — самопроверка не даст закончить")
    assert "←/→/Enter — переключить" in hint


def test_toggle_hint_updates_after_state_changes(monkeypatch):
    """hint строится от ПРИМЕНЁННОГО значения и меняется вслед за
    toggle_state-провайдером — переключение отражается сразу, без черновика."""
    reg = _reg_with_gate_answers(monkeypatch)
    state = {"gate": "on", "answers": "judge"}
    pal = _mk_palette(reg, state)

    hint_on = pal._toggle_hint(reg.lookup("gate"))
    assert hint_on.startswith("on — самопроверка не даст закончить")

    state["gate"] = "off"
    hint_off = pal._toggle_hint(reg.lookup("gate"))
    assert hint_off.startswith("off — самопроверка выключена")


def test_toggle_hint_carries_scope_inline(monkeypatch):
    """§6.1 модели уровней настроек (2026-07-21): область действия ВЕРНУЛАСЬ —
    внутрь клавишной части подсказки, не отдельным сегментом (отвергнутые
    подходы — отдельный сегмент-хвост / строка-легенда — см. дизайн §6.1).
    Несмонтированная палитра → width=0 → `_fit_hint` не усекает вовсе
    (см. первый guard), поэтому hint содержит область целиком."""
    reg = _reg_with_gate_answers(monkeypatch)
    pal = _mk_palette(reg, {"gate": "on", "answers": "judge"})
    pal._selected = len(pal._visible_rows())  # первая тоггл-строка (gate)

    hint = pal._toggle_hint(reg.lookup("gate"))

    assert hint.startswith("on — самопроверка не даст закончить")
    assert "←/→/Enter — переключить в текущей сессии" in hint


def test_fit_hint_priority_truncation():
    """Порядок жертв §6.1 (4 ступени): 1) хвост подсказки после последнего
    ' · '; 2) клавиши теряют область — «переключить в текущей сессии» →
    «переключить», область уходит ЦЕЛЫМ куском, не обрывком; 3) клавиши
    сжимаются до ←/→/Enter; 4) многоточие на смысле. Механика видима всегда."""
    from gc_tui.toggles import (
        _HINT_KEYS_TOGGLE_MEDIUM,
        _HINT_KEYS_TOGGLE_SHORT,
    )
    from gc_tui.widgets.command_palette import _HINT_KEYS_TOGGLE, _fit_hint

    value, keys = "on", _HINT_KEYS_TOGGLE
    hint = "самопроверка не даст закончить, пока запрос не закрыт · для сложных задач, где важна полнота"

    full = _fit_hint(value, hint, keys, width=200)
    assert "для сложных задач" in full
    assert "в текущей сессии" in full  # область на месте

    w120 = _fit_hint(value, hint, keys, width=120)
    assert "для сложных задач" not in w120  # 1) хвост ушёл первым
    assert "самопроверка" in w120
    assert "в текущей сессии" in w120  # область ещё цела в клавишах

    w90 = _fit_hint(value, hint, keys, width=90)
    assert "в текущей сессии" not in w90  # 2) область ушла ЦЕЛЫМ куском
    assert "в теку" not in w90  # не обрывок
    assert _HINT_KEYS_TOGGLE_MEDIUM in w90
    assert "самопроверка" in w90

    w80 = _fit_hint(value, hint, keys, width=80)
    assert _HINT_KEYS_TOGGLE_MEDIUM not in w80  # 3) клавиши сжались дальше
    assert _HINT_KEYS_TOGGLE_SHORT in w80

    w30 = _fit_hint(value, hint, keys, width=30)
    assert "…" in w30 and len(w30) <= 30  # 4) многоточие на смысле


@pytest.mark.asyncio
async def test_toggle_hint_scope_visible_wide_and_gracefully_truncated_narrow(
    tmp_path,
):
    """ГЛАВНЫЙ живой прогон §6.1: реальный pilot-mount палитры (не width=0
    несмонтированного виджета), тоггл выделен через обычный ввод `/gate`
    (как в e2e-тестах роутинга выше). Значение `off` — короткий смысл, чтобы
    область была видна целиком на широком терминале.

    Широкий терминал (140 колонок) — hint содержит «в текущей сессии»
    целиком. Узкий (70 колонок) — область усечена ЦЕЛЫМ куском (клавиши
    падают на `_HINT_KEYS_TOGGLE_MEDIUM`): «в текущей сессии» отсутствует
    полностью, обрывка «в теку…» нет."""
    pytest.importorskip("textual")

    app_wide, _ = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "off"\n'
    )
    async with app_wide.run_test(size=(140, 24)) as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/gate"
        await pilot.pause(0.1)
        palette = pilot.app.query_one(".palette")
        hint_wide = palette.query_one(".palette-hint").render().plain

        assert "переключить в текущей сессии" in hint_wide

    app_narrow, _ = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "off"\n'
    )
    async with app_narrow.run_test(size=(70, 24)) as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/gate"
        await pilot.pause(0.1)
        palette = pilot.app.query_one(".palette")
        hint_narrow = palette.query_one(".palette-hint").render().plain

        assert "в текущей сессии" not in hint_narrow  # ушла ЦЕЛЫМ куском
        assert "в теку" not in hint_narrow  # не обрывок
        assert "переключить" in hint_narrow  # клавиши-механика жива


def test_non_toggle_hint_unchanged():
    """Обычная (не toggle) строка — прежние _HINT_NO_ARGS/_HINT_WITH_ARGS, без
    изменений (toggle-хинт со значением её не задевает)."""
    from gc_tui.commands import Command
    from gc_tui.widgets.command_palette import (
        _HINT_NO_ARGS,
        _HINT_WITH_ARGS,
        _hint_for,
    )

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
    from gc_tui import palette as p
    from gc_tui import toggles as tg
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    five = tg.ToggleSpec(
        dotted="tui.fake_mode",
        values=tuple(
            tg.ToggleValue(f"v{i}", f"подсказка {i} · режим {i}", p.ACCENT)
            for i in range(5)
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
    assert tg.cycle_value("fake", "v4", 1) == "v0"  # цикл по кругу
    hint = pal._toggle_hint(reg.lookup("fake"))
    assert hint.startswith("v2 — подсказка 2")


def test_fit_hint_truncates_only_last_dot_segment_with_multiple_dots():
    """Carry ревью T3: подсказка с НЕСКОЛЬКИМИ ' · ' внутри — усечение хвоста
    режет только ПОСЛЕДНИЙ сегмент (rsplit-семантика §6), а не всё после
    первого разделителя."""
    from gc_tui.widgets.command_palette import _fit_hint

    value, keys = "v2", "Enter — применить · Esc — закрыть"
    hint = "шаг один · шаг два · режим N"

    fitted = _fit_hint(value, hint, keys, width=40)

    assert "режим N" not in fitted
    assert "шаг один · шаг два" in fitted


@pytest.mark.asyncio
async def test_palette_toggle_apply_failure_shows_flash_keeps_palette_and_value(
    tmp_path, monkeypatch
):
    """Находка ревью: apply_toggle кидает (напр. запись на диск невозможна) → флеш
    «✗ не сохранено: …» в футере, палитра остаётся открытой, значение не изменилось
    (config.yml не тронут). Монкипатчим apply_toggle в неймспейсе роутинга main.py —
    интеграция роутинга ←/→/Enter, не файловой записи. Оба маршрута (←/Enter) —
    один и тот же try/except в screens/main.py."""
    pytest.importorskip("textual")
    from gc_tui.screens import main as main_screen

    app, cfg_file = _app_with_config(
        tmp_path, 'tui:\n  finish_gate_mode: "on"\n'
    )

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
        assert (
            cfg_file.read_text(encoding="utf-8")
            == 'tui:\n  finish_gate_mode: "on"\n'
        )

        await pilot.press("enter")
        await pilot.pause(0.05)

        footer_text = str(pilot.app.query_one("#footer-left").render())
        assert "не сохранено" in footer_text
        assert pilot.app.query(".palette")
        assert (
            cfg_file.read_text(encoding="utf-8")
            == 'tui:\n  finish_gate_mode: "on"\n'
        )


@pytest.mark.asyncio
async def test_palette_navigation_does_not_change_footer_row_count(tmp_path):
    """Ревизия 7 (спека §2, §4.1): дети /gate,/answers видны ВСЕГДА под
    /rigor, независимо от выделения (видимость-по-выделению ревизии 5/6
    снесена — вся машинерия развёртывания группы по выделению удалена).
    Навигация ↑/↓ не меняет число toggle-строк подвала ни на одном шаге
    (сравниваем len до/после каждого press) — заменяет прежний тест «дети
    появляются/прячутся по группе» (ревизия 5). Реальные `pilot.press` —
    так двигается пользователь."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 40)) as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/"
        await pilot.pause(0.1)
        palette = pilot.app.query_one(".palette")

        def _names():
            return [c.name for c in palette._visible_toggle_rows()]

        assert palette.selected_command().name == "help"
        # level/profiles — новая группа «чем работаем» перед rigor (спека §12.1);
        # дети rigor (gate, answers) видны сразу, без навигации.
        assert _names() == ["level", "profiles", "rigor", "gate", "answers"]
        expected = len(_names())

        regular_count = len(palette._visible_rows())
        total_rows = regular_count + expected
        for _ in range(total_rows + 1):  # больше одного полного оборота
            await pilot.press("down")
            await pilot.pause(0.02)
            assert len(_names()) == expected

        for _ in range(total_rows + 1):
            await pilot.press("up")
            await pilot.pause(0.02)
            assert len(_names()) == expected


@pytest.mark.asyncio
async def test_group_separator_between_two_preset_groups(
    tmp_path, monkeypatch
):
    """Ревизия 7 (спека §4.1, генерик по `preset_keys`): между ДВУМЯ группами
    (тогглы-пресеты) — ровно один `.palette-group-sep`; до первой группы
    разделителя нет (граница «команды/подвал» уже несёт `.palette-separator`).
    Синтетический реестр с двумя preset-тогглами — сегодня в TOGGLES ровно
    одна preset-группа (`/rigor`), механизм рассчитан на будущее."""
    pytest.importorskip("textual")
    from gc_tui import palette as p
    from gc_tui import toggles as tg
    from gc_tui.app import TuiApp
    from gc_tui.commands import Command, CommandRegistry
    from gc_tui.widgets.command_palette import CommandPalette

    group_a = tg.ToggleSpec(
        values=(
            tg.ToggleValue("x", "hint x", p.INK),
            tg.ToggleValue("y", "hint y", p.ACCENT),
        ),
        state=lambda cfg: "x",
        label="группа A",
        summary="демо",
        preset_keys=("tui.zz_a_child",),
        palette_default=True,
    )
    group_b = tg.ToggleSpec(
        values=(
            tg.ToggleValue("p", "hint p", p.INK),
            tg.ToggleValue("q", "hint q", p.ACCENT),
        ),
        state=lambda cfg: "p",
        label="группа B",
        summary="демо",
        preset_keys=("tui.zz_b_child",),
        palette_default=True,
    )
    monkeypatch.setitem(tg.TOGGLES, "zzgroupa", group_a)
    monkeypatch.setitem(tg.TOGGLES, "zzgroupb", group_b)

    reg = CommandRegistry()
    reg.register(Command("help", (), "", "справка"))
    reg.register(Command("zzgroupa", (), "", "группа A", is_toggle=True))
    reg.register(Command("zzgroupb", (), "", "группа B", is_toggle=True))
    state = {"zzgroupa": "x", "zzgroupb": "p"}

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(
            reg, initial_filter="", toggle_state=lambda n: state[n]
        )
        await app.screen.mount(pal)
        await pilot.pause()

        seps = list(pal.query(".palette-group-sep"))
        assert len(seps) == 1  # ровно один — между группами

        toggle_rows = list(pal.query(".palette-toggle-row"))
        assert [r.render().plain.split()[0] for r in toggle_rows] == [
            "/zzgroupa",
            "/zzgroupb",
        ]
        # Разделитель группы строго между zzgroupa и zzgroupb.
        assert (
            toggle_rows[0].region.y
            < seps[0].region.y
            < toggle_rows[1].region.y
        )


@pytest.mark.asyncio
async def test_palette_filter_reveals_hidden_gate(tmp_path):
    """Набор «ga» → появляется /gate (существующий фильтр по startswith)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.commands import build_default_registry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = build_default_registry()
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(
            reg, initial_filter="ga", toggle_state=lambda n: "on"
        )
        await app.screen.mount(pal)
        await pilot.pause()
        assert [c.name for c in pal._visible_toggle_rows()] == ["gate"]


@pytest.mark.asyncio
async def test_regular_row_hint_unaffected_by_visible_toggles(tmp_path):
    """Carry ревью T3: e2e на реальном (смонтированном) реестре команд+тогглов —
    выделена ОБЫЧНАЯ строка при видимых toggle-строках в подвале → hint прежний
    (_HINT_NO_ARGS/_HINT_WITH_ARGS), не тоггловый."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.commands import build_default_registry
    from gc_tui.widgets.command_palette import _HINT_NO_ARGS, CommandPalette

    reg = build_default_registry()
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=(80, 40)) as pilot:
        pal = CommandPalette(
            reg, initial_filter="", toggle_state=lambda n: "on"
        )
        await app.screen.mount(pal)
        await pilot.pause()

        assert pal.query(
            ".palette-toggle-row"
        )  # тогглы реально видимы в подвале
        assert pal.selected_command().name == "help"  # выделена обычная строка

        hint = pal.query_one(".palette-hint")
        assert hint.render().plain == _HINT_NO_ARGS
