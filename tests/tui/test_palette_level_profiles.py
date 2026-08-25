"""`/level` и `/profiles` в палитре команд + футер из Router (спека
docs/specs/2026-07-23-profiles-design.md §12.1-§12.2).

Стиль — существующие тесты палитро-тогглов (tests/tui/test_palette_toggles.py):
прямые `assert`, `pytest.importorskip("textual")` перед импортами Textual-слоя.
"""
import pytest


def _wire_palette(app, reg, initial_filter=""):
    from tui.toggles import dynamic_toggle_hint, toggle_state
    from tui.widgets.command_palette import CommandPalette

    return CommandPalette(
        reg, initial_filter=initial_filter,
        toggle_state=lambda name: toggle_state(app, name),
        toggle_dynamic_hint=lambda name, value: dynamic_toggle_hint(app, name, value),
    )


# --- Группа «чем работаем» (/level, /profiles) — трек, разделитель, порядок ---

@pytest.mark.asyncio
async def test_level_and_profiles_form_new_group_above_rigor(tmp_path, monkeypatch):
    """/level и /profiles — новая группа над /rigor, отделённая от неё чертой
    (спека §12.1); gate/answers по-прежнему скрыты в пустом фильтре (§4.1)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from tui.app import TuiApp
    from tui.commands import build_default_registry

    app = TuiApp(threads_dir=str(tmp_path / "t"), config={
        "level": "default",
        "models": {"glm-local": {}},
        "slots": {"default": "glm-local"},
    })
    app._config_path = str(tmp_path / "config.yml")
    reg = build_default_registry()
    async with app.run_test(size=(100, 40)) as pilot:
        pal = _wire_palette(app, reg)
        await app.screen.mount(pal)
        await pilot.pause()

        rows = list(pal.query(".palette-toggle-row"))
        names = [r.render().plain.split()[0] for r in rows]
        # rigor всегда тащит своих детей gate/answers (спека §4.1) — видимый
        # подвал: level, profiles (новая группа), затем rigor + его дети.
        assert names == ["/level", "/profiles", "/rigor", "/gate", "/answers"]

        seps = list(pal.query(".palette-group-sep"))
        assert len(seps) == 1
        assert rows[1].region.y < seps[0].region.y < rows[2].region.y


def test_level_track_three_positions_light_default_max():
    """Трек /level — 3 позиции `○ ● ○` (текущая — default, средняя), подпись
    значения справа (спека §12.1: values=light·default·max)."""
    from tui.commands import Command, CommandRegistry
    from tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("level", ("models",), "", "уровень модели", is_toggle=True))
    pal = CommandPalette(reg, initial_filter="", toggle_state=lambda n: "default")

    row = pal._format_toggle_row(reg.lookup("level")).plain
    assert "○ ● ○" in row
    assert row.rstrip().endswith("default")


def test_level_track_colors_semantic():
    """Цвет ползунка — семантика значения (light=INK, default=RUN_COLOR,
    max=ACCENT — канон существующих тогглов, спека §12.1)."""
    from tui import palette as p
    from tui.commands import Command, CommandRegistry
    from tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("level", ("models",), "", "уровень модели", is_toggle=True))

    for value, color in (("light", p.INK), ("default", p.RUN_COLOR), ("max", p.ACCENT)):
        pal = CommandPalette(reg, initial_filter="", toggle_state=lambda n, v=value: v)
        text = pal._format_toggle_row(reg.lookup("level"))
        styles = [str(s.style) for s in text.spans if "bold" in str(s.style).lower()]
        assert any(color.lower() in st.lower() for st in styles), (value, styles)


def test_profiles_row_is_trackless():
    """/profiles — без шкалы: сразу значение-имя, без глифов ●/○ (спека §12.1)."""
    from tui.commands import Command, CommandRegistry
    from tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("profiles", (), "", "профиль", is_toggle=True))
    pal = CommandPalette(reg, initial_filter="", toggle_state=lambda n: "work")

    row = pal._format_toggle_row(reg.lookup("profiles")).plain
    assert "●" not in row and "○" not in row
    assert row.rstrip().endswith("work")


def test_profiles_row_empty_value_shows_none_words():
    """Пустое значение /profiles — словами «без профиля» (осмысленное состояние,
    ревизия 2026-07-28), не прочерк."""
    from tui.commands import Command, CommandRegistry
    from tui.widgets.command_palette import CommandPalette

    reg = CommandRegistry()
    reg.register(Command("profiles", (), "", "профиль", is_toggle=True))
    pal = CommandPalette(reg, initial_filter="", toggle_state=lambda n: "")

    row = pal._format_toggle_row(reg.lookup("profiles")).plain
    assert row.rstrip().endswith("без профиля")


# --- Тихий алиас `models` -> /level (механизм — прецедент /compact «сжать») ---

def test_models_alias_filters_to_level():
    """Набранное `/models` (фильтр палитры) находит `/level` — тихий алиас
    (спека §12.1): набравший «models» по привычке «сменить модель» попадает
    на уровень модели."""
    from tui.commands import build_default_registry
    from tui.widgets.command_palette import CommandPalette

    reg = build_default_registry()
    pal = CommandPalette(reg, initial_filter="models")
    rows = pal._visible_toggle_rows()
    assert [c.name for c in rows] == ["level"]


def test_models_alias_resolves_via_registry_lookup():
    """Полный ввод `/models` (Enter, минуя палитру) резолвится в canonical
    `level` через `CommandRegistry.lookup` — тот же механизм, что у `/сжать`."""
    from tui.commands import build_default_registry

    reg = build_default_registry()
    cmd = reg.lookup("models")
    assert cmd is not None and cmd.name == "level"


# --- Хинт /level — из Router, канон §1 ---------------------------------------

def test_level_hint_matches_canon_default_and_honest_fallback(tmp_path, monkeypatch):
    """Канон §12.1/§1 дословно: «default — glm-local · основная рабочая:
    повседневные задачи»; фоллбэк честно показывает стрелку «light → default»,
    не притворяется, что у light своя модель."""
    monkeypatch.chdir(tmp_path)
    from tui.app import TuiApp
    from tui.toggles import level_value_hint

    app = TuiApp(config={
        "level": "default",
        "models": {"glm-local": {}},
        "slots": {"default": "glm-local"},
    })
    app._config_path = str(tmp_path / "config.yml")

    assert level_value_hint(app, "default") == (
        "glm-local · основная рабочая: повседневные задачи"
    )
    assert level_value_hint(app, "light") == (
        "glm-local (light → default) · быстрая и дешёвая: простые задачи"
    )
    assert level_value_hint(app, "max") == (
        "glm-local (max → default) · самая сильная: сложные задачи"
    )


def test_level_hint_legacy_mode(tmp_path, monkeypatch):
    """Легаси-режим (нет merged-реестра) — /level виден, хинт честно
    помечает «легаси: уровни появятся с реестром моделей» (спека §12.1)."""
    monkeypatch.chdir(tmp_path)
    from tui.app import TuiApp
    from tui.toggles import level_value_hint

    app = TuiApp(config={"model": "glm-5.2-fp8"})
    app._config_path = str(tmp_path / "config.yml")

    hint = level_value_hint(app, "default")
    assert hint == "glm-5.2-fp8 · легаси: уровни появятся с реестром моделей"


def test_level_hint_legacy_force(tmp_path, monkeypatch):
    """Легаси-форс (`--model` поверх непустого реестра) — резолв уровней
    отключён на этот запуск, хинт честно об этом сообщает; выбор персистится
    (session override не трогается этой веткой — только показ)."""
    monkeypatch.chdir(tmp_path)
    from tui.app import TuiApp
    from tui.toggles import level_value_hint

    app = TuiApp(config={
        "model": "glm-5.2-fp8",
        "models": {"glm-local": {}},
        "slots": {"default": "glm-local"},
        "cli_explicit_keys": ["model"],
    })
    app._config_path = str(tmp_path / "config.yml")

    hint = level_value_hint(app, "default")
    assert hint == "glm-5.2-fp8 · --model: уровни отключены на этот запуск"


# --- /profiles: цикл по алфавиту, тёзка — одна позиция, состав+источник -----

def test_profile_values_deduplicate_namesake_project_wins(tmp_path, monkeypatch):
    """Объединённый список — алфавитный, тёзка (одинаковое имя в обоих
    корнях) — ОДНА позиция; побеждает проектный (спека §3.1/§12.1)."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_dir = tmp_path / "base"
    (base_dir / "profiles").mkdir(parents=True)
    (base_dir / "profiles" / "alpha.yml").write_text("models: {}\n", encoding="utf-8")
    (base_dir / "profiles" / "work.yml").write_text("models: {}\n", encoding="utf-8")
    (project_dir / ".gc" / "profiles").mkdir(parents=True)
    (project_dir / ".gc" / "profiles" / "work.yml").write_text("models: {}\n", encoding="utf-8")

    from tui.app import TuiApp
    from tui.toggles import profile_values, _discovered_profiles

    app = TuiApp(config={"profile": ""})
    app._config_path = str(base_dir / "config.yml")

    values = profile_values(app)
    assert [v.value for v in values] == ["alpha", "work"]   # тёзка — одна позиция, алфавит
    assert _discovered_profiles(app)["work"].root_kind == "project"


def test_profiles_cycle_wraps_alphabetically(tmp_path, monkeypatch):
    """←/→ циклят по объединённому алфавитному списку, по кругу (спека §12.1)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "alpha.yml").write_text("models: {}\n", encoding="utf-8")
    (tmp_path / "profiles" / "work.yml").write_text("models: {}\n", encoding="utf-8")

    from tui.app import TuiApp
    from tui.toggles import next_toggle_value

    app = TuiApp(config={"profile": "alpha"})
    app._config_path = str(tmp_path / "config.yml")

    assert next_toggle_value(app, "profiles", 1) == "work"
    app.set_session_override("profile", "work")
    assert next_toggle_value(app, "profiles", 1) == "alpha"   # по кругу


def test_profiles_hint_shows_slots_and_project_source(tmp_path, monkeypatch):
    """Хинт-канон §12.1 дословно: «light flash · default glm-local ·
    проектный» — состав слотов активного профиля + источник."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    (project_dir / ".gc" / "profiles").mkdir(parents=True)
    (project_dir / ".gc" / "profiles" / "work.yml").write_text("models: {}\n", encoding="utf-8")

    from tui.app import TuiApp
    from tui.toggles import profile_value_hint

    app = TuiApp(config={
        "profile": "work",
        "models": {"flash": {}, "glm-local": {}},
        "slots": {"light": "flash", "default": "glm-local"},
    })
    app._config_path = str(tmp_path / "base" / "config.yml")

    hint = profile_value_hint(app, "work")
    assert hint == "light flash · default glm-local · проектный · Enter — открыть список"


def test_profiles_hint_warns_about_mcp_rebuild(tmp_path, monkeypatch):
    """Цена переключения (§12.1): профиль, переопределяющий MCP-серверы, —
    предупреждение в хинте. Секция во ВХОДНОМ документе называется `mcp: servers:`
    (плоский `mcp-servers` существует только в снимке flatten_app_config, а
    литеральный верхнеуровневый `mcp-servers:` схема профиля отвергает)."""
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / "base" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "corp.yml").write_text(
        "mcp:\n"
        "  servers:\n"
        "    jira:\n"
        "      transport: sse\n"
        "      url: http://jira.local/sse\n"
        "      description: corp jira\n"
        "      server-name: jira\n"
        "    confluence:\n"
        "      transport: sse\n"
        "      url: http://wiki.local/sse\n"
        "      description: corp wiki\n"
        "      server-name: confluence\n",
        encoding="utf-8",
    )

    from tui.app import TuiApp
    from tui.toggles import profile_value_hint

    app = TuiApp(config={"profile": "corp"})
    app._config_path = str(tmp_path / "base" / "config.yml")

    assert "переключение пересоберёт MCP: 2 серверов" in profile_value_hint(app, "corp")


def test_profiles_empty_state_hint_verbatim(tmp_path, monkeypatch):
    """Пустое состояние — текст спеки §12.1 дословно, и ТОЛЬКО когда дискавери
    не нашёл ни одного файла (ревизия 2026-07-28)."""
    monkeypatch.chdir(tmp_path)
    from tui.app import TuiApp
    from tui.toggles import profile_value_hint

    app = TuiApp(config={})
    app._config_path = str(tmp_path / "config.yml")

    assert profile_value_hint(app, "") == (
        "профили не настроены · создать: ~/.gc/profiles/<имя>.yml · "
        "пример — в /settings → Профили"
    )


def test_profiles_not_selected_hint_lists_available(tmp_path, monkeypatch):
    """Профили существуют, но не выбран → НЕ «не настроены» (это был баг смока),
    а «не выбран · доступно: <имена> · Enter — выбрать» (спека §12.1, ревизия)."""
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / "base" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "smoke.yml").write_text("models: {}\n", encoding="utf-8")

    from tui.app import TuiApp
    from tui.toggles import profile_value_hint

    app = TuiApp(config={})
    app._config_path = str(tmp_path / "base" / "config.yml")

    assert profile_value_hint(app, "") == "не выбран · доступно: smoke · Enter — открыть список"


# --- Футер: действующая модель следующего хода из Router --------------------

@pytest.mark.asyncio
async def test_footer_shows_router_model_not_stale_flat_key(tmp_path, monkeypatch):
    """Футер берёт активную модель из Router (§12.2), а не устаревший плоский
    `cfg["model"]` (база наследования, не действующая модель при живом реестре)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "t"), config={
        "model": "STALE-LEGACY-NAME",
        "models": {"glm-local": {}},
        "slots": {"default": "glm-local"},
        "level": "default",
    })
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        right = scr._compose_footer_right().plain
        assert right.startswith("● glm-local")
        assert "STALE-LEGACY-NAME" not in right


@pytest.mark.asyncio
async def test_footer_updates_on_level_switch(tmp_path, monkeypatch):
    """Футер обновляется при смене /level — БЕЗ пересборки executor (§6.4):
    Router-витрина читает effective_config()/сессионный уровень на каждый вызов."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "t"), config={
        "model": "STALE",
        "models": {"glm-local": {}, "glm-max": {}},
        "slots": {"default": "glm-local", "max": "glm-max"},
        "level": "default",
    })
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        assert scr._compose_footer_right().plain.startswith("● glm-local")
        app.set_session_override("level", "max")
        assert scr._compose_footer_right().plain.startswith("● glm-max")


# --- Fix round 1 (ревью): битый профиль-файл не роняет hint /profiles ------

def test_profiles_hint_survives_syntactically_broken_yaml(tmp_path, monkeypatch):
    """Синтаксически битый профиль-файл (юзер правит руками, волна A), найденный
    `discover_profiles` по glob, не должен ронять навигацию/рендер `/profiles` —
    MCP-предупреждение просто не показывается (yaml.YAMLError наравне с OSError)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "broken.yml").write_text(
        "models:\n  flash: [unterminated\n", encoding="utf-8",
    )

    from tui.app import TuiApp
    from tui.toggles import profile_value_hint, profile_values

    app = TuiApp(config={"profile": "broken"})
    app._config_path = str(tmp_path / "config.yml")

    assert [v.value for v in profile_values(app)] == ["broken"]   # дискавери не падает
    hint = profile_value_hint(app, "broken")   # не бросает YAMLError
    assert "MCP" not in hint   # предупреждения нет — просто не досчиталось


# --- Fix round 2 (ревью): легаси-форс доходит до реального executor'а -------

def _stub_create_run_context(monkeypatch, captured):
    class _Ctx:
        tools_data = {}
        mcp_status = {}
        logging_path = "/tmp/agent_main_test.log"

    class _Ex:
        def __init__(self, ctx):
            pass

        def stop_run(self):
            pass

    def _fake_create_run_context(cfg, **kw):
        captured.update(kw)
        return _Ctx()

    monkeypatch.setattr("config.build_auth_connector", lambda cfg: None)
    monkeypatch.setattr("run_context.create_run_context", _fake_create_run_context)
    monkeypatch.setattr("agent_executor.AgentExecutor", _Ex)


def test_build_real_executor_passes_force_legacy_when_cli_model_forces_it(tmp_path, monkeypatch):
    """`--model` поверх непустого реестра — TUI обязана форсировать легаси в
    РЕАЛЬНОМ executor'е (не только в хинте `/level`, спека §2/§12.1): CLI/server
    уже передают `cli_forces_legacy_model(cfg)`, TUI была единственным пробелом."""
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "t"), config={
        "model": "cli-forced-model",
        "models": {"glm-local": {}},
        "slots": {"default": "glm-local"},
        "cli_explicit_keys": ["model"],
    })
    captured: dict = {}
    _stub_create_run_context(monkeypatch, captured)
    app._build_real_executor()

    assert captured["force_legacy"] is True


def test_build_real_executor_force_legacy_false_without_cli_model_override(tmp_path, monkeypatch):
    """Без CLI-форса (обычный запуск с реестром) — `force_legacy=False`, резолв
    уровней/алиасов работает как обычно."""
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "t"), config={
        "models": {"glm-local": {}},
        "slots": {"default": "glm-local"},
    })
    captured: dict = {}
    _stub_create_run_context(monkeypatch, captured)
    app._build_real_executor()

    assert captured["force_legacy"] is False


# --- Пикер /profiles (спека §12.1, ревизия 2026-07-28) ----------------------

def _picker_app(tmp_path, monkeypatch, *, base_profile="", session=None):
    """App с двумя глобальными профилями (alpha, smoke) для тестов пикера."""
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / "base" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "smoke.yml").write_text(
        "models:\n  gc:\n    url: http://x\n    token: \"\"\nslots:\n  default: gc\n", encoding="utf-8")
    (profiles_dir / "alpha.yml").write_text("models: {}\n", encoding="utf-8")

    from tui.app import TuiApp
    app = TuiApp(config={"profile": base_profile})
    app._config_path = str(tmp_path / "base" / "config.yml")
    if session is not None:
        app._session_overrides.update(session)
    return app


def test_profile_picker_options_none_first_marks(tmp_path, monkeypatch):
    """«— без профиля» первым; профили по алфавиту с источником; конфиг-указатель —
    «(как в конфиге)» в скобках без разделителя; активность — ОТДЕЛЬНЫЙ 3-й
    элемент (статус-колонка пикера), не часть описания (решение смока 2026-08-10)."""
    from tui.toggles import profile_picker_options

    app = _picker_app(tmp_path, monkeypatch, base_profile="smoke")
    options = profile_picker_options(app)
    assert options[0][0] == "без профиля"
    assert [name for name, _, _ in options[1:]] == ["alpha", "smoke"]
    by_name = {name: (desc, status) for name, desc, status in options}
    assert "глобальный" in by_name["alpha"][0]
    assert by_name["smoke"][0] == "глобальный (как в конфиге)"
    assert by_name["smoke"][1] == "● активен"
    assert by_name["alpha"][1] == ""
    assert by_name["без профиля"][1] == ""


def test_profile_picker_options_none_marked_active_when_no_profile(tmp_path, monkeypatch):
    from tui.toggles import profile_picker_options

    app = _picker_app(tmp_path, monkeypatch)
    options = profile_picker_options(app)
    assert options[0][0] == "без профиля" and options[0][2] == "● активен"


def test_profile_picker_options_exactly_one_active_row(tmp_path, monkeypatch):
    """Инвариант: активной может быть РОВНО одна строка — либо «без профиля»,
    либо один профиль, никогда ни одна и никогда две сразу."""
    from tui.toggles import profile_picker_options

    for i, base_profile in enumerate(("", "smoke", "alpha")):
        case_dir = tmp_path / f"case{i}"
        case_dir.mkdir()
        app = _picker_app(case_dir, monkeypatch, base_profile=base_profile)
        options = profile_picker_options(app)
        active = [name for name, _, status in options if status]
        assert len(active) == 1, (base_profile, options)


def test_profile_picker_hint_shows_target_slots_not_effective(tmp_path, monkeypatch):
    """Хинт ховера — слоты ЦЕЛЕВОГО профиля из его файла (выбор ещё не применён),
    не слоты эффективного конфига."""
    from tui.toggles import profile_picker_hint

    app = _picker_app(tmp_path, monkeypatch)   # эффективный конфиг: слотов нет
    hint = profile_picker_hint(app, "smoke")
    assert "default gc" in hint and "глобальный" in hint


def test_profile_picker_hint_none_value(tmp_path, monkeypatch):
    from tui.toggles import profile_picker_hint

    app = _picker_app(tmp_path, monkeypatch)
    assert profile_picker_hint(app, "без профиля") == (
        "настройки берутся только из config.yml — файл профиля не подмешивается")


def test_profiles_command_has_dynamic_picker_wiring():
    from tui.commands import build_default_registry

    cmd = build_default_registry().lookup("profiles")
    assert cmd.args == "[профиль]"
    assert cmd.arg_options_provider is not None
    assert cmd.arg_hint_provider is not None


@pytest.mark.asyncio
async def test_arg_picker_dynamic_footer_hint_follows_selection(tmp_path):
    pytest.importorskip("textual")
    from textual.app import App
    from tui.widgets.arg_picker import ArgPicker

    hints = {"без профиля": "чистый конфиг", "smoke": "default gc · глобальный"}
    picker = ArgPicker([("без профиля", "работать чистым конфигом"), ("smoke", "глобальный")],
                       hint_provider=lambda v: hints.get(v, ""))

    class _Host(App):
        def compose(self):
            yield picker

    async with _Host().run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        texts = [str(w.render()) for w in picker.query(".palette-hint")]
        assert any("чистый конфиг" in t for t in texts)
        picker.move_selection(1)
        await pilot.pause()
        texts = [str(w.render()) for w in picker.query(".palette-hint")]
        assert any("default gc" in t for t in texts)


def _bare_trace_screen(monkeypatch, app):
    from tui.screens.main import TraceScreen
    scr = TraceScreen()
    monkeypatch.setattr(type(scr), "app", property(lambda self: app), raising=False)
    return scr


def test_cmd_profiles_dash_removes_session_profile(tmp_path, monkeypatch):
    app = _picker_app(tmp_path, monkeypatch, session={"profile": "smoke"})
    calls = []
    app.switch_session_profile = lambda name: calls.append(name)
    scr = _bare_trace_screen(monkeypatch, app)
    scr._cmd_profiles("без профиля")
    assert calls == [""]


def test_cmd_profiles_known_name_switches(tmp_path, monkeypatch):
    app = _picker_app(tmp_path, monkeypatch)
    calls = []
    app.switch_session_profile = lambda name: calls.append(name)
    scr = _bare_trace_screen(monkeypatch, app)
    scr._cmd_profiles("smoke")
    assert calls == ["smoke"]


def test_cmd_profiles_unknown_name_honest_refusal(tmp_path, monkeypatch):
    app = _picker_app(tmp_path, monkeypatch)
    calls, hints = [], []
    app.switch_session_profile = lambda name: calls.append(name)
    scr = _bare_trace_screen(monkeypatch, app)
    scr._show_footer_hint = lambda text: hints.append(text)
    scr._cmd_profiles("wrok")
    assert calls == []
    assert hints and "wrok" in hints[0] and "smoke" in hints[0]


def _raiser(exc):
    def _fn(_name):
        raise exc
    return _fn


def test_cmd_profiles_write_failure_shows_hint_not_crash(tmp_path, monkeypatch):
    """Каталог тредов недоступен: `apply_session_override` откатывает и ре-райзит
    OSError. Из обработчика клавиш исключение уронило бы приложение — команда
    показывает то же «✗ не сохранено», что и палитра."""
    app = _picker_app(tmp_path, monkeypatch)
    hints = []
    app.switch_session_profile = _raiser(OSError("read-only"))
    scr = _bare_trace_screen(monkeypatch, app)
    scr._show_footer_hint = lambda text: hints.append(text)
    scr._cmd_profiles("smoke")
    assert hints and "не сохранено" in hints[0] and "read-only" in hints[0]


def test_cmd_profiles_dash_write_failure_shows_hint_not_crash(tmp_path, monkeypatch):
    app = _picker_app(tmp_path, monkeypatch, session={"profile": "smoke"})
    hints = []
    app.switch_session_profile = _raiser(OSError("disk full"))
    scr = _bare_trace_screen(monkeypatch, app)
    scr._show_footer_hint = lambda text: hints.append(text)
    scr._cmd_profiles("без профиля")
    assert hints and "не сохранено" in hints[0]


def test_cmd_profiles_vanished_profile_shows_hint_not_crash(tmp_path, monkeypatch):
    """Файл профиля исчез между листингом и выбором — пере-мерж внутри
    `switch_session_profile` бросает ValueError."""
    app = _picker_app(tmp_path, monkeypatch)
    hints = []
    app.switch_session_profile = _raiser(ValueError("профиль не найден"))
    scr = _bare_trace_screen(monkeypatch, app)
    scr._show_footer_hint = lambda text: hints.append(text)
    scr._cmd_profiles("smoke")
    assert hints and "не сохранено" in hints[0]


def test_cmd_profiles_no_arg_opens_picker(tmp_path, monkeypatch):
    app = _picker_app(tmp_path, monkeypatch)
    opened = []
    scr = _bare_trace_screen(monkeypatch, app)
    scr._open_profiles_picker = lambda *a, **k: opened.append(True)
    scr._cmd_profiles("")
    assert opened == [True]


def test_session_removal_overrides_base_pointer(tmp_path, monkeypatch):
    """§9.1 ревизия: сессионное "" при конфиг-указателе — чистый конфиг
    (пере-мерж БЕЗ профиля), а не база с профилем."""
    monkeypatch.chdir(tmp_path)
    base_dir = tmp_path / "base"
    (base_dir / "profiles").mkdir(parents=True)
    (base_dir / "profiles" / "work.yml").write_text(
        "models:\n  flash:\n    url: http://x\n    token: \"\"\nslots:\n  default: flash\n",
        encoding="utf-8")
    (base_dir / "config.yml").write_text(
        "profile: work\nbackend:\n  completions_url: http://b\n", encoding="utf-8")

    from config import load_config_ex
    from tui.app import TuiApp

    cfg, _meta = load_config_ex(str(base_dir / "config.yml"))
    assert cfg.get("models")   # профиль подмешан

    app = TuiApp(config=cfg)
    app._config_path = str(base_dir / "config.yml")
    app.set_session_override("profile", "")
    eff = app.effective_config()
    assert not eff.get("models")          # оверлей профиля снят
    assert eff.get("profile") == ""


# --- Task 15: /level посреди хода — уведомление об отложенном применении -----
#
# Симметрично `switch_session_profile` (§9.1, `test_session_profile_level.py`):
# сессионный оверрайд применяется сразу (палитра меняет значение мгновенно),
# но запросы текущего хода всё ещё идут на прежнем пине — уведомление честно
# говорит, что новый уровень подхватится СО СЛЕДУЮЩЕГО хода.


def _level_app(tmp_path):
    from tui.app import TuiApp
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("level: default\n", encoding="utf-8")
    app = TuiApp(config={"level": "default"})
    app._config_path = str(cfg_file)
    app._threads_dir = str(tmp_path / "threads")
    app.tui_state.active_task_id = "t1"
    return app


def test_apply_level_toggle_defers_notice_while_run_is_live(tmp_path):
    pytest.importorskip("textual")
    from runtime_notices import clear_runtime_notices, collect_runtime_notices
    from tui.toggles import apply_toggle

    clear_runtime_notices()
    app = _level_app(tmp_path)

    class _LiveRunner:
        def is_running(self):
            return True

    app.active_runner = _LiveRunner()
    apply_toggle(app, "level", "max")

    assert app.effective_config()["level"] == "max"   # применилось сразу (§ выбор)
    codes = [n.code for n in collect_runtime_notices()]
    assert "level_switch_deferred" in codes
    msg = next(n.message for n in collect_runtime_notices() if n.code == "level_switch_deferred")
    assert "max" in msg and "завершения текущей задачи" in msg


def test_apply_level_toggle_no_notice_without_active_run(tmp_path):
    pytest.importorskip("textual")
    from runtime_notices import clear_runtime_notices, collect_runtime_notices
    from tui.toggles import apply_toggle

    clear_runtime_notices()
    app = _level_app(tmp_path)
    apply_toggle(app, "level", "max")

    codes = [n.code for n in collect_runtime_notices()]
    assert "level_switch_deferred" not in codes


def test_apply_level_toggle_no_notice_after_run_finished(tmp_path):
    pytest.importorskip("textual")
    from runtime_notices import clear_runtime_notices, collect_runtime_notices
    from tui.toggles import apply_toggle

    clear_runtime_notices()
    app = _level_app(tmp_path)

    class _FinishedRunner:
        def is_running(self):
            return False

    app.active_runner = _FinishedRunner()
    apply_toggle(app, "level", "max")

    codes = [n.code for n in collect_runtime_notices()]
    assert "level_switch_deferred" not in codes
