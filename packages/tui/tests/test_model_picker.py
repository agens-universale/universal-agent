"""Пикер `/model` и команда `/model` (спека model-knob §6.3, §3.2, §3.3,
§4.4): состав и порядок строк, знак модели слева, «активна», описания,
подсказки,
легаси и отказ на неизвестное имя."""

import pytest
from gc_testkit.tui_support import tui_settings

_AUTO_ON = {
    "backend.api_mode": "chat_completions",
    "agent.auto_routing": True,
}
# Пять моделей: три на уровнях (каждая на своём), две вне уровней.
_ONE_PER_LEVEL = {
    "models": {
        "glm-flash": {},
        "glm-4.6": {},
        "giga-max": {},
        "qwen-coder": {},
        "deepseek-v3": {},
    },
    "slots": {"light": "glm-flash", "default": "glm-4.6", "max": "giga-max"},
}
# giga-max держит два уровня; в реестре модели уровней стоят не первыми.
_TWO_LEVELS = {
    "models": {
        "qwen-coder": {},
        "giga-max": {},
        "deepseek-v3": {},
        "glm-flash": {},
    },
    "slots": {"light": "glm-flash", "default": "giga-max", "max": "giga-max"},
}


def _app(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp

    app = TuiApp(settings=tui_settings(config))
    app._config_path = str(tmp_path / "config.yml")
    return app


def _values(options):
    return ["—" if o.separator else o.value for o in options]


def _status(options):
    return {o.value: o.status for o in options if not o.separator}


def test_picker_order_auto_levels_rest(tmp_path, monkeypatch):
    """auto, черта, модели уровней по младшему уровню (каждая один раз),
    черта, остальные в порядке реестра."""
    from gc_tui.services.model_picker import model_picker_options

    app = _app(
        tmp_path, monkeypatch, {"model": "auto", **_AUTO_ON, **_TWO_LEVELS}
    )
    assert _values(model_picker_options(app)) == [
        "auto",
        "—",
        "glm-flash",
        "giga-max",
        "—",
        "qwen-coder",
        "deepseek-v3",
    ]
    auto = model_picker_options(app)[0]
    assert auto.desc == "модель выбирается автоматически по ходу работы"
    assert auto.enabled


def test_picker_empty_block_has_no_own_separator(tmp_path, monkeypatch):
    """Нет моделей вне уровней — второй черты нет."""
    from gc_tui.services.model_picker import model_picker_options

    config = {
        "models": {"glm-flash": {}, "glm-4.6": {}},
        "slots": {"light": "glm-flash", "default": "glm-4.6"},
    }
    app = _app(tmp_path, monkeypatch, {**_AUTO_ON, **config})
    assert _values(model_picker_options(app)) == [
        "auto",
        "—",
        "glm-flash",
        "glm-4.6",
    ]


def test_picker_dots_right_aligned_to_name(tmp_path, monkeypatch):
    """Колонка знака — по самой многоуровневой модели, точки прижаты к
    имени, один пробел до него; цвет точки — уровень."""
    pytest.importorskip("textual")
    from gc_tui import palette as p
    from gc_tui.services.model_picker import model_picker_options
    from gc_tui.widgets.arg_picker import ArgPicker

    app = _app(
        tmp_path, monkeypatch, {"model": "auto", **_AUTO_ON, **_TWO_LEVELS}
    )
    options = model_picker_options(app)
    leads = {o.value: o.lead.plain for o in options if not o.separator}
    assert leads == {
        "auto": "●",
        "glm-flash": "●",
        "giga-max": "●●",
        "qwen-coder": "●",
        "deepseek-v3": "●",
    }
    giga = next(o for o in options if o.value == "giga-max")
    assert [str(s.style) for s in giga.lead.spans] == [
        p.RUN_COLOR,
        p.ACCENT,
    ]
    picker = ArgPicker(list(options))
    rows = {
        o.value: picker._format_row(o).plain
        for o in options
        if not o.separator
    }
    starts = {v: row.index(v) for v, row in rows.items()}
    assert len(set(starts.values())) == 1
    for value, row in rows.items():
        assert row[starts[value] - 2 : starts[value]] == "● "


def test_picker_marks_current_active(tmp_path, monkeypatch):
    """«активна» — на выборе действующих настроек; сессионный выбор
    переносит отметку, слово уровня отмечает модель своего слота, а
    переданные настройки (база для «Настроек») — своё значение."""
    from gc_tui.services.model_picker import model_picker_options

    app = _app(
        tmp_path,
        monkeypatch,
        {"model": "glm-4.6", **_AUTO_ON, **_ONE_PER_LEVEL},
    )
    assert _status(model_picker_options(app)) == {
        "auto": "",
        "glm-flash": "",
        "glm-4.6": "активна",
        "giga-max": "",
        "qwen-coder": "",
        "deepseek-v3": "",
    }
    app.apply_model_choice("qwen-coder")
    marked = [v for v, s in _status(model_picker_options(app)).items() if s]
    assert marked == ["qwen-coder"]
    base = model_picker_options(app, settings=app.base_settings())
    assert [v for v, s in _status(base).items() if s] == ["glm-4.6"]
    app.apply_model_choice("max")
    marked = [v for v, s in _status(model_picker_options(app)).items() if s]
    assert marked == ["giga-max"]


def test_picker_desc_names_model_role(tmp_path, monkeypatch):
    """Описание строки: у auto — режим, у модели одного уровня — имя
    уровня и его роль («light · быстрая…»), у модели нескольких уровней —
    их список без ролей, без уровня — пусто (смок, раунд 2, п. 6)."""
    from gc_tui.services.model_picker import AUTO_DESC, model_picker_options
    from gc_tui.toggles import LEVEL_ROLE_TEXT

    app = _app(tmp_path, monkeypatch, {**_AUTO_ON, **_ONE_PER_LEVEL})
    descs = {
        o.value: o.desc for o in model_picker_options(app) if not o.separator
    }
    assert descs == {
        "auto": AUTO_DESC,
        "glm-flash": f"light · {LEVEL_ROLE_TEXT['light']}",
        "glm-4.6": f"default · {LEVEL_ROLE_TEXT['default']}",
        "giga-max": f"max · {LEVEL_ROLE_TEXT['max']}",
        "qwen-coder": "",
        "deepseek-v3": "",
    }
    app = _app(tmp_path, monkeypatch, {**_AUTO_ON, **_TWO_LEVELS})
    giga = next(o for o in model_picker_options(app) if o.value == "giga-max")
    assert giga.desc == "уровни default и max"
    all_three = {
        "models": {"giga-max": {}},
        "slots": {
            "light": "giga-max",
            "default": "giga-max",
            "max": "giga-max",
        },
    }
    app = _app(tmp_path, monkeypatch, all_three)
    giga = next(o for o in model_picker_options(app) if o.value == "giga-max")
    assert giga.desc == "уровни light, default и max"


_ADDRESSED = {
    "models": {
        "glm-flash": {
            "model": "zai/glm-4.5-flash",
            "url": "https://api.z.ai/v1/chat/completions",
            "token": "",
        },
        "giga-max": {
            "url": "https://giga.example.ru:8443/v1/chat",
            "token": "",
        },
    },
    "slots": {"light": "glm-flash", "default": "giga-max", "max": "giga-max"},
}


def test_picker_hint_is_model_id_and_host(tmp_path, monkeypatch):
    """Подсказка — без имени и уровней: id у провайдера и хост адреса;
    id, равный алиасу, опускается."""
    from gc_tui.services.model_picker import model_hint

    app = _app(tmp_path, monkeypatch, {**_AUTO_ON, **_ADDRESSED})
    assert model_hint(app, "glm-flash") == "zai/glm-4.5-flash · api.z.ai"
    assert model_hint(app, "giga-max") == "giga.example.ru:8443"
    assert model_hint(app, None) == ""


def test_picker_auto_hint_names_pairs(tmp_path, monkeypatch):
    """У auto — пары автовыбора, без имени."""
    from gc_tui.services.model_picker import model_hint

    app = _app(
        tmp_path, monkeypatch, {"model": "auto", **_AUTO_ON, **_TWO_LEVELS}
    )
    # safe_level по умолчанию — default: одна пара light + default.
    assert model_hint(app, "auto") == ("пара автовыбора: glm-flash и giga-max")


def test_picker_auto_hint_follows_router_pairs(tmp_path, monkeypatch):
    """Пары подсказки — те, которыми ходит роутер: лестница пар — обе;
    выродившаяся лестница (одна модель на default и max) — одна пара."""
    from gc_tui.services.model_picker import model_hint

    ladder = {"agent.auto_routing_safe_level": "auto", **_AUTO_ON}
    app = _app(tmp_path, monkeypatch, {**ladder, **_ONE_PER_LEVEL})
    assert model_hint(app, "auto") == (
        "пары автовыбора: glm-flash и glm-4.6, glm-4.6 и giga-max"
    )
    app = _app(tmp_path, monkeypatch, {**ladder, **_TWO_LEVELS})
    assert model_hint(app, "auto") == ("пара автовыбора: glm-flash и giga-max")


def test_picker_auto_disabled_with_reason(tmp_path, monkeypatch):
    """Автовыбор недоступен — строка auto видна, но выбрать её нельзя;
    подсказка называет причину."""
    from gc_tui.services.model_picker import (
        AUTO_DESC,
        model_hint,
        model_picker_options,
    )

    app = _app(
        tmp_path,
        monkeypatch,
        {
            "backend.api_mode": "chat_completions",
            "agent.auto_routing": False,
            **_ONE_PER_LEVEL,
        },
    )
    auto = model_picker_options(app)[0]
    assert auto.value == "auto" and not auto.enabled
    assert auto.desc == AUTO_DESC
    assert model_hint(app, "auto") == (
        "автовыбор недоступен: выключен настройкой auto_routing"
    )


def test_picker_breaker_model_hint(tmp_path, monkeypatch):
    """Модель в предохранителе — видна и выбираема (ошибка будет честной),
    к подсказке добавлено «недоступна в этой сессии» (Review Focus 5)."""
    from gc_tui.services.model_picker import (
        model_hint,
        model_picker_options,
    )

    app = _app(tmp_path, monkeypatch, {**_AUTO_ON, **_ADDRESSED})
    app._circuit_breaker.trip("glm-flash", "401")
    flash = next(
        o for o in model_picker_options(app) if o.value == "glm-flash"
    )
    assert flash.enabled
    assert model_hint(app, "glm-flash") == (
        "zai/glm-4.5-flash · api.z.ai · недоступна в этой сессии"
    )


def test_picker_row_status_column_follows_name(tmp_path, monkeypatch):
    """Строка пикера: имя, колонка статуса сразу за ним (у неактивных —
    пусто), затем описание; описание — SUB_INK."""
    pytest.importorskip("textual")
    from gc_tui import palette as p
    from gc_tui.services.model_picker import model_picker_options
    from gc_tui.widgets.arg_picker import ArgPicker

    app = _app(
        tmp_path,
        monkeypatch,
        {"model": "glm-4.6", **_AUTO_ON, **_ONE_PER_LEVEL},
    )
    options = [o for o in model_picker_options(app) if not o.separator]
    picker = ArgPicker(options)
    rows = {o.value: picker._format_row(o) for o in options}
    active = rows["glm-4.6"].plain
    flash = rows["glm-flash"].plain
    status_at = active.index("активна")
    assert active[:status_at].rstrip().endswith("glm-4.6")
    assert flash[status_at : status_at + len("активна")].strip() == ""
    assert active.index("default ·") == flash.index("light ·")
    desc = rows["glm-flash"]
    start = desc.plain.index("light ·")
    styles = {(s.start, str(s.style)) for s in desc.spans}
    assert (start, p.SUB_INK) in styles


def test_picker_legacy_single_row(tmp_path, monkeypatch):
    """Пустой реестр: одна строка плоской модели (активна), выбор не
    действует — ни она, ни auto не выбираются."""
    from gc_tui.services.model_picker import (
        AUTO_DESC,
        model_hint,
        model_picker_options,
    )

    app = _app(tmp_path, monkeypatch, {"agent.model": "glm-5.2-fp8"})
    options = model_picker_options(app)
    rows = [o for o in options if not o.separator]
    assert [o.value for o in rows] == ["auto", "glm-5.2-fp8"]
    assert not rows[0].enabled and rows[0].desc == AUTO_DESC
    assert rows[1].status == "активна" and not rows[1].enabled
    assert model_hint(app, "glm-5.2-fp8") == (
        "легаси: выбор модели появится с реестром моделей"
    )


def test_model_value_is_sign_of_current_choice(tmp_path, monkeypatch):
    """Значение для палитры: знак + имя, у auto — «● auto»."""
    from gc_tui.services.model_picker import model_value

    app = _app(
        tmp_path,
        monkeypatch,
        {"model": "glm-flash", **_AUTO_ON, **_TWO_LEVELS},
    )
    assert model_value(app).plain == "● glm-flash"
    app.apply_model_choice("giga-max")
    assert model_value(app).plain == "●● giga-max"
    app.apply_model_choice("auto")
    assert model_value(app).plain == "● auto"


def _bare_trace_screen(monkeypatch, app):
    from gc_tui.screens.main import TraceScreen

    scr = TraceScreen()
    monkeypatch.setattr(
        type(scr), "app", property(lambda self: app), raising=False
    )
    return scr


def test_model_command_rejects_unknown_name(tmp_path, monkeypatch):
    """`/model <имя>` с именем вне реестра — отказ, в сессию не пишется."""
    app = _app(tmp_path, monkeypatch, {**_AUTO_ON, **_ONE_PER_LEVEL})
    scr = _bare_trace_screen(monkeypatch, app)
    hints = []
    scr._show_footer_refusal = lambda text: hints.append(text)
    scr._cmd_model("giga-mx")
    assert hints == ["✗ нет такой модели: giga-mx"]
    assert "model" not in app._session_overrides


def test_model_command_auto_unavailable_names_reason(tmp_path, monkeypatch):
    app = _app(
        tmp_path,
        monkeypatch,
        {"agent.auto_routing": False, **_ONE_PER_LEVEL},
    )
    scr = _bare_trace_screen(monkeypatch, app)
    hints = []
    scr._show_footer_refusal = lambda text: hints.append(text)
    scr._cmd_model("auto")
    assert hints == [
        "/model auto — автовыбор моделей недоступен: "
        "выключен настройкой auto_routing"
    ]
    assert "model" not in app._session_overrides


def test_model_command_with_name_applies(tmp_path, monkeypatch):
    """`/model <имя>` — сессионный выбор; без имени — пикер."""
    app = _app(tmp_path, monkeypatch, {**_AUTO_ON, **_ONE_PER_LEVEL})
    scr = _bare_trace_screen(monkeypatch, app)
    applied, opened = [], []
    app.apply_model_choice = lambda choice: applied.append(choice)
    scr._open_command_picker = lambda name: opened.append(name)
    # Отметка смены модели — тест ленты (test_trace_model_mark.py).
    scr._mark_model_choice = app.apply_model_choice
    scr._cmd_model("deepseek-v3")
    scr._cmd_model("")
    assert applied == ["deepseek-v3"]
    assert opened == ["model"]


def test_modes_section_keeps_model_row():
    """«Режимы работы» держат строку модели первой: без трека, Enter —
    список (спека §6.6); ручки уровня нет."""
    from gc_tui.toggles import TOGGLES, modes_section_names

    assert modes_section_names()[:2] == ["model", "mode"]
    spec = TOGGLES["model"]
    assert spec.settings_label == "модель по умолчанию"
    assert spec.group == "model"
    assert not spec.show_track and spec.opens_picker
    assert not spec.palette_default
    assert not TOGGLES["profiles"].palette_default
