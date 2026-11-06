"""Настройки → «Режимы работы»: строка «модель по умолчанию» (спека
model-knob §6.6): знак записанного выбора, Enter — список моделей цели
записи, подсказка называет файл, плашка — значение и файл."""

import pytest

pytest.importorskip("textual")

import yaml
from gc_testkit.tui_support import open_settings_section, tui_settings

_REGISTRY = (
    "models:\n"
    "  flash:\n"
    '    model: "google/flash"\n'
    '    url: "https://llm.example.com/v1/chat/completions"\n'
    '    token: ""\n'
    "  big:\n"
    '    model: "big-x"\n'
    '    url: "https://llm.example.com/v1/chat/completions"\n'
    '    token: ""\n'
    "  solo:\n"
    '    model: "solo-x"\n'
    '    url: "https://llm.example.com/v1/chat/completions"\n'
    '    token: ""\n'
    "slots:\n"
    '  default: "flash"\n'
    '  max: "big"\n'
)


def _app_with_config(tmp_path, yaml_text):
    from gc_tui import app as app_mod

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(yaml_text, encoding="utf-8")
    app = app_mod.TuiApp(settings=tui_settings({}))
    app._config_path = str(cfg_file)
    app.reload_config()
    return app, cfg_file


def _write_profile(tmp_path, name, text):
    path = tmp_path / "profiles" / f"{name}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def _open_modes(pilot):
    from gc_tui.screens.settings import SettingsScreen

    app = pilot.app
    scr = SettingsScreen(
        settings=app.base_settings(),
        config_path=app._config_path,
        on_apply=app.reload_config,
    )
    app.push_screen(scr)
    await pilot.pause()
    open_settings_section(scr, "Режимы работы")
    await pilot.pause(0.1)
    return scr


def _model_row_index():
    from gc_tui.toggles import modes_section_names

    return modes_section_names().index("model")


def _texts(scr, selector):
    return [str(w.render()) for w in scr.query(selector)]


async def _pick(pilot, scr, value):
    """Открыть список Enter'ом и выбрать `value` стрелками и Enter."""
    scr._modes_idx = _model_row_index()
    await pilot.press("enter")
    await pilot.pause()
    picker = scr._modes_picker
    for _ in range(12):
        if picker.selected_value() == value:
            break
        await pilot.press("down")
    assert picker.selected_value() == value
    await pilot.press("enter")
    await pilot.pause(0.1)


async def test_model_row_label_and_value_sign(tmp_path, monkeypatch):
    """Голова — `model · модель по умолчанию`, значение — знак записанного
    выбора: слово уровня показывает модель своего слота с её точками."""
    from gc_tui.palette import RUN_COLOR

    monkeypatch.chdir(tmp_path)
    app, _ = _app_with_config(tmp_path, 'model: "default"\n' + _REGISTRY)
    async with app.run_test() as pilot:
        scr = await _open_modes(pilot)

        text = scr._mode_row_text("model", selected=False)

        assert "model · модель по умолчанию" in text.plain
        assert text.plain.rstrip().endswith("● flash")
        dot = text.plain.rindex("●")
        assert any(
            span.start <= dot < span.end and span.style == RUN_COLOR
            for span in text.spans
        )


async def test_enter_opens_picker_and_arrows_do_nothing(tmp_path, monkeypatch):
    """←/→ на строке модели ничего не пишут; Enter открывает список
    моделей над подсказкой раздела, Esc его закрывает и из секции не
    уводит."""
    from gc_tui.widgets.arg_picker import ArgPicker

    monkeypatch.chdir(tmp_path)
    app, cfg_file = _app_with_config(
        tmp_path, 'model: "default"\n' + _REGISTRY
    )
    original = cfg_file.read_text(encoding="utf-8")
    async with app.run_test() as pilot:
        scr = await _open_modes(pilot)
        scr._modes_idx = _model_row_index()

        await pilot.press("right", "left")
        await pilot.pause(0.1)
        assert cfg_file.read_text(encoding="utf-8") == original
        assert not list(scr.query(ArgPicker))

        await pilot.press("enter")
        await pilot.pause()
        pickers = list(scr.query(ArgPicker))
        assert len(pickers) == 1
        hint = list(scr.query(".set-modes-hint"))[-1]
        assert pickers[0].region.y < hint.region.y
        values = [o.value for o in pickers[0]._options if not o.separator]
        assert values == ["auto", "flash", "big", "solo"]
        # Клавиши называет футер экрана: Tab здесь не работает.
        picker_text = " ".join(
            str(w.render()) for w in pickers[0].query("Static")
        )
        assert "flash" in picker_text
        assert "Tab" not in picker_text
        # Список открыт на записанном значении: Enter его не меняет.
        assert pickers[0].selected_value() == "flash"

        await pilot.press("escape")
        await pilot.pause()
        assert not list(scr.query(ArgPicker))
        assert scr._view != "menu"
        assert cfg_file.read_text(encoding="utf-8") == original


async def test_picker_marks_written_value_not_session_choice(
    tmp_path, monkeypatch
):
    """«активна» стоит на записанном выборе, а не на выборе сессии:
    секция правит файл."""
    from gc_tui.services.model_picker import ACTIVE_STATUS

    monkeypatch.chdir(tmp_path)
    app, _ = _app_with_config(tmp_path, 'model: "solo"\n' + _REGISTRY)
    app.apply_model_choice("big")
    async with app.run_test() as pilot:
        scr = await _open_modes(pilot)
        scr._modes_idx = _model_row_index()
        await pilot.press("enter")
        await pilot.pause()

        active = [
            o.value
            for o in scr._modes_picker._options
            if o.status == ACTIVE_STATUS
        ]
        assert active == ["solo"]


async def test_picker_never_offers_auto_allowed_only_by_session_profile(
    tmp_path, monkeypatch
):
    """База с одной моделью, профиль сессии с парой: палитра видит `auto`,
    но секция правит базу — `auto` в её списке недоступен и Enter на нём
    ничего не пишет."""
    monkeypatch.chdir(tmp_path)
    _write_profile(
        tmp_path,
        "pair",
        yaml.safe_dump(
            {
                "models": {
                    "fast": {
                        "url": "http://fast/v1/chat/completions",
                        "token": "",
                    },
                    "mid": {
                        "url": "http://mid/v1/chat/completions",
                        "token": "",
                    },
                },
                "slots": {"light": "fast", "default": "mid"},
            }
        ),
    )
    app, cfg_file = _app_with_config(
        tmp_path,
        "backend:\n  api_mode: chat_completions\n"
        '  completions_url: "http://base/v1/chat/completions"\n'
        'model: "default"\n'
        'models:\n  solo:\n    model: "solo-x"\n',
    )
    app.switch_session_profile("pair")
    from gc_tui.services.model_picker import model_picker_options

    session_auto = [o for o in model_picker_options(app) if o.value == "auto"]
    assert session_auto[0].enabled  # сессия его видит
    original = cfg_file.read_text(encoding="utf-8")
    async with app.run_test() as pilot:
        scr = await _open_modes(pilot)
        scr._modes_idx = _model_row_index()
        await pilot.press("enter")
        await pilot.pause()
        picker = scr._modes_picker
        for _ in range(6):
            if picker.selected_value() == "auto":
                break
            await pilot.press("up")
        assert picker.selected_value() == "auto"
        assert picker.selected_disabled()

        await pilot.press("enter")
        await pilot.pause(0.1)

        assert cfg_file.read_text(encoding="utf-8") == original
        assert scr._modes_picker is not None  # список остался открытым


async def test_hint_names_target_profile(tmp_path, monkeypatch):
    """Активен профиль — подсказка называет профиль и его файл."""
    from gc_tui.screens.settings import _shown_path

    monkeypatch.chdir(tmp_path)
    work = _write_profile(tmp_path, "work", _REGISTRY)
    app, _ = _app_with_config(tmp_path, 'profile: "work"\nmodel: "default"\n')
    async with app.run_test() as pilot:
        scr = await _open_modes(pilot)
        scr._modes_idx = _model_row_index()

        hint = scr._modes_hint_text()

        assert hint == (
            "google/flash · llm.example.com · Enter — выбрать из списка · "
            f"запишется в профиль work ({_shown_path(work)})"
        )


async def test_hint_names_target_project(tmp_path, monkeypatch):
    """Проектный конфиг сам задаёт `model` — запись уйдёт туда."""
    from gc_tui.screens.settings import _shown_path

    project = tmp_path / "project"
    (project / ".gc").mkdir(parents=True)
    project_cfg = project / ".gc" / "config.yml"
    project_cfg.write_text('model: "max"\n', encoding="utf-8")
    monkeypatch.chdir(project)
    app, _ = _app_with_config(tmp_path, 'model: "default"\n' + _REGISTRY)
    async with app.run_test() as pilot:
        scr = await _open_modes(pilot)
        scr._modes_idx = _model_row_index()

        hint = scr._modes_hint_text()

        assert hint.endswith(
            f"запишется в проектный конфиг ({_shown_path(project_cfg)})"
        )
        assert hint.startswith("big-x · llm.example.com · ")


async def test_hint_names_target_base(tmp_path, monkeypatch):
    """Ни профиля, ни проектного ключа — основной конфиг."""
    from gc_tui.screens.settings import _shown_path

    monkeypatch.chdir(tmp_path)
    app, cfg_file = _app_with_config(tmp_path, 'model: "solo"\n' + _REGISTRY)
    async with app.run_test() as pilot:
        scr = await _open_modes(pilot)
        scr._modes_idx = _model_row_index()

        hint = scr._modes_hint_text()

        assert hint == (
            "solo-x · llm.example.com · Enter — выбрать из списка · "
            f"запишется в основной конфиг ({_shown_path(cfg_file)})"
        )


async def test_write_goes_to_active_profile_file(tmp_path, monkeypatch):
    """Активен профиль — выбор ложится в его файл, основной не тронут."""
    monkeypatch.chdir(tmp_path)
    work = _write_profile(tmp_path, "work", _REGISTRY)
    app, cfg_file = _app_with_config(
        tmp_path, 'profile: "work"\nmodel: "default"\n'
    )
    base_before = cfg_file.read_text(encoding="utf-8")
    async with app.run_test() as pilot:
        scr = await _open_modes(pilot)

        await _pick(pilot, scr, "big")

        assert yaml.safe_load(work.read_text(encoding="utf-8"))["model"] == (
            "big"
        )
        assert cfg_file.read_text(encoding="utf-8") == base_before
        assert scr._modes_picker is None
        assert "● big" in scr._mode_row_text("model", False).plain


async def test_saved_plaque_names_value_and_file(tmp_path, monkeypatch):
    """После записи — плашка: значение и файл."""
    from gc_tui.screens.settings import _shown_path

    monkeypatch.chdir(tmp_path)
    app, cfg_file = _app_with_config(
        tmp_path, 'model: "default"\n' + _REGISTRY
    )
    async with app.run_test() as pilot:
        scr = await _open_modes(pilot)

        await _pick(pilot, scr, "big")

        assert yaml.safe_load(cfg_file.read_text(encoding="utf-8"))[
            "model"
        ] == ("big")
        flashes = _texts(scr, "#set-flash")
        expected = (
            "«Модель по умолчанию» — big, записано в "
            f"{_shown_path(cfg_file.resolve())}"
        )
        assert any(expected in f for f in flashes), flashes


async def test_override_warning_names_session_model(tmp_path, monkeypatch):
    """Записанное перекрыто сессионным `/model` — предупреждение говорит,
    что действует и чем перекрыто."""
    monkeypatch.chdir(tmp_path)
    app, _ = _app_with_config(tmp_path, 'model: "default"\n' + _REGISTRY)
    app.apply_model_choice("solo")
    async with app.run_test() as pilot:
        scr = await _open_modes(pilot)

        await _pick(pilot, scr, "big")

        notices = _texts(scr, "#set-notice")
        assert any(
            "записано в" in n
            and "но действует «solo»" in n
            and "выбором сессии /model" in n
            for n in notices
        ), notices


async def test_menu_preview_shows_model(tmp_path, monkeypatch):
    """Превью «Режимов работы» в меню называет модель словами, а не
    словом уровня, и без знака: сводка — серый текст, точки в ней не
    различают уровни."""
    monkeypatch.chdir(tmp_path)
    app, _ = _app_with_config(tmp_path, 'model: "max"\n' + _REGISTRY)
    async with app.run_test() as pilot:
        from gc_tui.screens.settings import _MODES, SettingsScreen

        scr = SettingsScreen(
            settings=app.base_settings(),
            config_path=app._config_path,
            on_apply=app.reload_config,
        )
        app.push_screen(scr)
        await pilot.pause()

        preview = scr._row_value(_MODES).plain

        assert preview.startswith("модель: big ·")


async def test_missing_active_profile_file_refuses_not_base(
    tmp_path, monkeypatch
):
    """Активный профиль, чьего файла нет на диске: в основной конфиг молча
    не пишем — отказ с причиной, подсказка говорит то же."""
    monkeypatch.chdir(tmp_path)
    work = _write_profile(tmp_path, "work", _REGISTRY)
    app, cfg_file = _app_with_config(
        tmp_path, 'profile: "work"\nmodel: "default"\n'
    )
    base_before = cfg_file.read_text(encoding="utf-8")
    async with app.run_test() as pilot:
        scr = await _open_modes(pilot)
        work.unlink()
        scr._modes_idx = _model_row_index()

        assert scr._modes_hint_text().endswith("файл профиля work не найден")
        assert scr._write_model_default("big") is None
        await pilot.pause()

        assert cfg_file.read_text(encoding="utf-8") == base_before
        notices = _texts(scr, "#set-notice")
        assert any(
            "✗ не сохранено: файл профиля work не найден" in n for n in notices
        ), notices


@pytest.mark.parametrize("size", ((60, 30), (100, 24)))
async def test_long_model_list_fits_and_scrolls(tmp_path, monkeypatch, size):
    """≈40 моделей в узком окне: список модели по умолчанию не выше 80%
    экрана, рамка целиком на экране, выбранная строка видна над подвалом
    и в конце списка, и в начале (смок, раунд 2, п. 8)."""
    monkeypatch.chdir(tmp_path)
    models = "".join(
        f"  m{i:02d}:\n"
        f'    model: "vendor/m{i:02d}"\n'
        '    url: "https://llm.example.com/v1/chat/completions"\n'
        '    token: ""\n'
        for i in range(40)
    )
    yaml_text = (
        'model: "default"\nmodels:\n' + models + 'slots:\n  default: "m00"\n'
    )
    app, _ = _app_with_config(tmp_path, yaml_text)
    async with app.run_test(size=size) as pilot:
        scr = await _open_modes(pilot)
        scr._modes_idx = _model_row_index()
        await pilot.press("enter")
        await pilot.pause()
        picker = scr._modes_picker
        assert picker.outer_size.height <= int(size[1] * 0.8)
        assert picker.region.bottom <= size[1]
        for key in ("up", "down"):  # заворот в конец, затем в начало
            await pilot.press(key)
            await pilot.pause()
            await pilot.pause()
            row = picker.query_one(".palette-row.selected")
            footer = picker.query_one(".palette-footer")
            assert row.region.height == 1, picker.selected_value()
            assert row.region.y > picker.region.y
            assert row.region.bottom <= footer.region.y
