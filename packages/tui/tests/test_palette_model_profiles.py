"""`/model` и `/profile` в палитре команд + футер из Router (спеки
docs/specs/2026-07-23-profiles-design.md §12.1-§12.2 и
docs/specs/2026-10-25-model-knob-design.md §6.2).

Стиль — существующие тесты палитро-тогглов (tests/tui/test_palette_toggles.py):
прямые `assert`, `pytest.importorskip("textual")` перед импортами Textual-слоя.
"""

import pytest
from gc_testkit.tui_support import tui_settings


def _wire_palette(app, reg, initial_filter=""):
    from gc_tui.toggles import (
        dynamic_toggle_hint,
        effective_toggle_values,
        toggle_state,
    )
    from gc_tui.widgets.command_palette import CommandPalette

    return CommandPalette(
        reg,
        initial_filter=initial_filter,
        toggle_state=lambda name: toggle_state(app, name),
        toggle_dynamic_hint=lambda name, value: dynamic_toggle_hint(
            app, name, value
        ),
        toggle_values=lambda name: effective_toggle_values(app, name),
        command_value=lambda cmd: (
            cmd.value_provider(app) if cmd.value_provider else None
        ),
    )


def _registry_app(tmp_path, monkeypatch, config=None):
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings(
            config
            or {
                "model": "glm-4.6",
                "models": {"glm-4.6": {}, "giga-max": {}},
                "slots": {"default": "glm-4.6", "max": "giga-max"},
            }
        ),
    )
    app._config_path = str(tmp_path / "config.yml")
    return app


# --- /model и /profile — обычные команды, значение справа (спека §6.2) -----


@pytest.mark.asyncio
async def test_model_and_profiles_are_plain_commands_with_values_right(
    tmp_path, monkeypatch
):
    """`/model` и `/profile` — строки верхнего списка, не ручки подвала;
    в колонке клавиш — текущее значение: знак + имя модели и «без профиля»."""
    pytest.importorskip("textual")
    from gc_tui import palette as p
    from gc_tui.commands import build_default_registry

    app = _registry_app(tmp_path, monkeypatch)
    reg = build_default_registry()
    assert not reg.lookup("model").is_toggle
    assert not reg.lookup("profile").is_toggle
    async with app.run_test(size=(100, 40)) as pilot:
        pal = _wire_palette(app, reg)
        await app.screen.mount(pal)
        await pilot.pause()

        regular = [c.name for c in pal._visible_rows()]
        assert regular[:4] == ["help", "sessions", "model", "profile"]
        model_row = pal._format_row(reg.lookup("model"))
        profiles_row = pal._format_row(reg.lookup("profile"))
        help_row = pal._format_row(reg.lookup("help"))

    assert model_row.plain.rstrip().endswith("● glm-4.6")
    assert profiles_row.plain.rstrip().endswith("без профиля")
    # значения и клавиши — одна правая колонка, прижатая к краю
    assert len(model_row.plain) == len(str(help_row))
    dot = model_row.plain.rindex("●")
    styles = [str(s.style) for s in model_row.spans if s.start <= dot < s.end]
    assert p.ACCENT in styles


def test_knob_group_is_only_rigor_family():
    """Подвал ручек — только `/rigor` и его дети; ручки уровня нет."""
    from gc_tui.commands import build_default_registry
    from gc_tui.toggles import TOGGLES
    from gc_tui.widgets.command_palette import CommandPalette

    reg = build_default_registry()
    pal = CommandPalette(reg, initial_filter="")
    assert [c.name for c in pal._visible_toggle_rows()] == [
        "rigor",
        "gate",
        "answers",
    ]
    assert "level" not in TOGGLES
    assert reg.lookup("level") is None


# --- Тихий алиас `models` -> /model ------------------------------------------


def test_models_alias_filters_to_model():
    """Набранное `/models` (фильтр палитры) находит `/model` — тихий алиас."""
    from gc_tui.commands import build_default_registry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = build_default_registry()
    pal = CommandPalette(reg, initial_filter="models")
    assert [c.name for c in pal._visible_rows()] == ["model"]


def test_models_alias_resolves_via_registry_lookup():
    """Полный ввод `/models` резолвится в canonical `model` через
    `CommandRegistry.lookup` — тот же механизм, что у `/сжать`."""
    from gc_tui.commands import build_default_registry

    reg = build_default_registry()
    cmd = reg.lookup("models")
    assert cmd is not None and cmd.name == "model"


# --- Тихий алиас `profiles` -> /profile --------------------------------------


def test_profiles_alias_resolves_and_stays_hidden():
    """Прежнее имя `/profiles` резолвится в `/profile`, но отдельной строкой
    палитры не стоит: фильтр `profiles` находит одну строку `/profile`."""
    from gc_tui.commands import build_default_registry
    from gc_tui.widgets.command_palette import CommandPalette

    reg = build_default_registry()
    assert reg.lookup("profiles").name == "profile"
    assert "profiles" not in [c.name for c in reg.list_commands()]
    pal = CommandPalette(reg, initial_filter="profiles")
    assert [c.name for c in pal._visible_rows()] == ["profile"]
    pal = CommandPalette(reg, initial_filter="prof")
    assert [c.name for c in pal._visible_rows()] == ["profile"]


@pytest.mark.asyncio
async def test_typed_profiles_alias_runs_profile_command(
    tmp_path, monkeypatch
):
    """Набранное `/profiles <имя>` — та же команда, что `/profile <имя>`."""
    pytest.importorskip("textual")
    app = _registry_app(tmp_path, monkeypatch)
    async with app.run_test() as pilot:
        hints = []
        pilot.app.screen._show_footer_refusal = hints.append
        await pilot.app.screen._handle_command("profiles nope")
        assert hints and 'профиль "nope" не найден' in hints[0]


# --- /profile: цикл по алфавиту, тёзка — одна позиция, состав+источник -----


def test_profile_values_deduplicate_namesake_project_wins(
    tmp_path, monkeypatch
):
    """Объединённый список — алфавитный, тёзка (одинаковое имя в обоих
    корнях) — ОДНА позиция; побеждает проектный (спека §3.1/§12.1)."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base_dir = tmp_path / "base"
    (base_dir / "profiles").mkdir(parents=True)
    (base_dir / "profiles" / "alpha.yml").write_text(
        "models: {}\n", encoding="utf-8"
    )
    (base_dir / "profiles" / "work.yml").write_text(
        "models: {}\n", encoding="utf-8"
    )
    (project_dir / ".gc" / "profiles").mkdir(parents=True)
    (project_dir / ".gc" / "profiles" / "work.yml").write_text(
        "models: {}\n", encoding="utf-8"
    )

    from gc_tui.app import TuiApp
    from gc_tui.toggles import _discovered_profiles, profile_values

    app = TuiApp(settings=tui_settings({"profile": ""}))
    app._config_path = str(base_dir / "config.yml")

    values = profile_values(app)
    assert [v.value for v in values] == [
        "alpha",
        "work",
    ]  # тёзка — одна позиция, алфавит
    assert _discovered_profiles(app)["work"].root_kind == "project"


def test_profiles_cycle_wraps_alphabetically(tmp_path, monkeypatch):
    """←/→ циклят по объединённому алфавитному списку, по кругу (спека §12.1)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "alpha.yml").write_text(
        "models: {}\n", encoding="utf-8"
    )
    (tmp_path / "profiles" / "work.yml").write_text(
        "models: {}\n", encoding="utf-8"
    )

    from gc_tui.app import TuiApp
    from gc_tui.toggles import next_toggle_value

    # Сессионный выбор пере-собирает базу под профилем — нужен сам config.yml.
    (tmp_path / "config.yml").write_text(
        'profile: "alpha"\n', encoding="utf-8"
    )
    app = TuiApp(settings=tui_settings({"profile": "alpha"}))
    app._config_path = str(tmp_path / "config.yml")

    assert next_toggle_value(app, "profiles", 1) == "work"
    app.set_session_override("profile", "work")
    assert next_toggle_value(app, "profiles", 1) == "alpha"  # по кругу


def test_profiles_hint_shows_slots_and_project_source(tmp_path, monkeypatch):
    """Хинт-канон §12.1 дословно: «light flash · default glm-local ·
    проектный» — состав слотов активного профиля + источник."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    (project_dir / ".gc" / "profiles").mkdir(parents=True)
    (project_dir / ".gc" / "profiles" / "work.yml").write_text(
        "models: {}\n", encoding="utf-8"
    )

    from gc_tui.app import TuiApp
    from gc_tui.toggles import profile_value_hint

    app = TuiApp(
        settings=tui_settings(
            {
                "profile": "work",
                "models": {"flash": {}, "glm-local": {}},
                "slots": {"light": "flash", "default": "glm-local"},
            }
        )
    )
    app._config_path = str(tmp_path / "base" / "config.yml")

    hint = profile_value_hint(app, "work")
    assert (
        hint
        == "light flash · default glm-local · проектный · Enter — открыть список"
    )


def test_profiles_hint_warns_about_mcp_rebuild(tmp_path, monkeypatch):
    """Цена переключения (§12.1): профиль, переопределяющий MCP-серверы, —
    предупреждение в хинте. Секция во ВХОДНОМ документе называется `mcp: servers:`
    (верхнеуровневое имя `mcp-servers` — только плоское имя ключа, а
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

    from gc_tui.app import TuiApp
    from gc_tui.toggles import profile_value_hint

    app = TuiApp(settings=tui_settings({"profile": "corp"}))
    app._config_path = str(tmp_path / "base" / "config.yml")

    assert "переключение пересоберёт MCP: 2 серверов" in profile_value_hint(
        app, "corp"
    )


def test_profiles_empty_state_hint_verbatim(tmp_path, monkeypatch):
    """Пустое состояние — текст спеки §12.1 дословно, и ТОЛЬКО когда дискавери
    не нашёл ни одного файла (ревизия 2026-07-28)."""
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp
    from gc_tui.toggles import profile_value_hint

    app = TuiApp(settings=tui_settings({}))
    app._config_path = str(tmp_path / "config.yml")

    assert profile_value_hint(app, "") == (
        "профили не настроены · создать: ~/.gc/profiles/<имя>.yml · пример — в /settings → Профили"
    )


def test_profiles_not_selected_hint_lists_available(tmp_path, monkeypatch):
    """Профили существуют, но не выбран → НЕ «не настроены» (это был баг смока),
    а «не выбран · доступно: <имена> · Enter — выбрать» (спека §12.1, ревизия)."""
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / "base" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "smoke.yml").write_text("models: {}\n", encoding="utf-8")

    from gc_tui.app import TuiApp
    from gc_tui.toggles import profile_value_hint

    app = TuiApp(settings=tui_settings({}))
    app._config_path = str(tmp_path / "base" / "config.yml")

    assert (
        profile_value_hint(app, "")
        == "не выбран · доступно: smoke · Enter — открыть список"
    )


# --- Футер: действующая модель следующего хода из Router --------------------


@pytest.mark.asyncio
async def test_footer_shows_router_model_not_stale_flat_key(
    tmp_path, monkeypatch
):
    """Футер берёт активную модель из Router (§12.2), а не устаревший плоский
    `cfg["model"]` (база наследования, не действующая модель при живом реестре)."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings(
            {
                "agent.model": "STALE-LEGACY-NAME",
                "models": {"glm-local": {}},
                "slots": {"default": "glm-local"},
                "model": "default",
            }
        ),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        right = scr._compose_footer_right().plain
        assert right.startswith("● glm-local")
        assert "STALE-LEGACY-NAME" not in right


@pytest.mark.asyncio
async def test_footer_updates_on_level_switch(tmp_path, monkeypatch):
    """Футер обновляется при смене /level — БЕЗ пересборки executor (§6.4):
    Router-витрина читает effective_settings()/сессионный уровень на каждый вызов."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings(
            {
                "agent.model": "STALE",
                "models": {"glm-local": {}, "glm-max": {}},
                "slots": {"default": "glm-local", "max": "glm-max"},
                "model": "default",
            }
        ),
    )
    app._config_path = str(tmp_path / "config.yml")
    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        assert scr._compose_footer_right().plain.startswith("● glm-local")
        app.set_session_override("model", "max")
        assert scr._compose_footer_right().plain.startswith("● glm-max")


# --- Fix round 1 (ревью): битый профиль-файл не роняет hint /profile ------


def test_profiles_hint_survives_syntactically_broken_yaml(
    tmp_path, monkeypatch
):
    """Синтаксически битый профиль-файл (юзер правит руками, волна A), найденный
    `discover_profiles` по glob, не должен ронять навигацию/рендер `/profile` —
    MCP-предупреждение просто не показывается (yaml.YAMLError наравне с OSError)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "broken.yml").write_text(
        "models:\n  flash: [unterminated\n",
        encoding="utf-8",
    )

    from gc_tui.app import TuiApp
    from gc_tui.toggles import profile_value_hint, profile_values

    app = TuiApp(settings=tui_settings({"profile": "broken"}))
    app._config_path = str(tmp_path / "config.yml")

    assert [v.value for v in profile_values(app)] == [
        "broken"
    ]  # дискавери не падает
    hint = profile_value_hint(app, "broken")  # не бросает YAMLError
    assert "MCP" not in hint  # предупреждения нет — просто не досчиталось


# --- Fix round 2 (ревью): легаси-форс доходит до реального executor'а -------


def _stub_create_run_context(monkeypatch, captured):
    class _Ctx:
        # У настоящего RunContext бэкенд есть всегда; двойнику он нужен, потому что
        # TuiApp включает diff-журнал сам (attach_to_context) — раньше это делало
        # ядро внутри acreate_run_context, то есть внутри подменённой функции.
        os_backend = None
        tools_data = {}
        mcp_status = {}
        logging_path = "/tmp/agent_main_test.log"

    class _Ex:
        def __init__(self, ctx):
            pass

        def stop_run(self):
            pass

    async def _fake_acreate_run_context(_cfg, **kw):
        captured.update(kw)
        return _Ctx()

    monkeypatch.setattr(
        "gc_core.config_modules.auth.build_auth_connector", lambda cfg: None
    )
    monkeypatch.setattr(
        "gc_core.run_context.acreate_run_context", _fake_acreate_run_context
    )
    monkeypatch.setattr("gc_core.agent_executor.AgentExecutor", _Ex)


def test_build_real_executor_passes_force_legacy_when_cli_model_forces_it(
    tmp_path, monkeypatch
):
    """`--model` поверх непустого реестра — TUI обязана форсировать легаси в
    РЕАЛЬНОМ executor'е (не только в хинте `/level`, спека §2/§12.1): CLI/server
    уже передают `cli_forces_legacy_model(cfg)`, TUI была единственным пробелом."""
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        # `--model` — слой CLI поверх загруженного (как `apply_cli_overrides`).
        settings=tui_settings(
            {
                "models": {"glm-local": {}},
                "slots": {"default": "glm-local"},
            }
        ).with_overrides({"agent.model": "cli-forced-model"}, layer="cli"),
    )
    captured: dict = {}
    _stub_create_run_context(monkeypatch, captured)
    app._build_real_executor()

    assert captured["force_legacy"] is True


def test_build_real_executor_force_legacy_false_without_cli_model_override(
    tmp_path, monkeypatch
):
    """Без CLI-форса (обычный запуск с реестром) — `force_legacy=False`, резолв
    уровней/алиасов работает как обычно."""
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings(
            {
                "models": {"glm-local": {}},
                "slots": {"default": "glm-local"},
            }
        ),
    )
    captured: dict = {}
    _stub_create_run_context(monkeypatch, captured)
    app._build_real_executor()

    assert captured["force_legacy"] is False


# --- Пикер /profile (спека §12.1, ревизия 2026-07-28) ----------------------


def _picker_app(tmp_path, monkeypatch, *, base_profile="", session=None):
    """App с двумя глобальными профилями (alpha, smoke) для тестов пикера.

    `config.yml` кладётся НА ДИСК, а не только в `_config_path`: пикер и гейт
    выбора судят профиль настоящей сборкой, и без базового файла не собирается
    ни один профиль — стенд без него врал бы «не применится» про здоровых."""
    monkeypatch.chdir(tmp_path)
    profiles_dir = tmp_path / "base" / "profiles"
    profiles_dir.mkdir(parents=True)
    (tmp_path / "base" / "config.yml").write_text(
        "backend:\n  completions_url: http://b\n", encoding="utf-8"
    )
    (profiles_dir / "smoke.yml").write_text(
        # token: "" — правило пары: свой адрес идёт со своим токеном (сервер ключа не требует).
        'models:\n  gc:\n    url: http://x\n    token: ""\nslots:\n  default: gc\n',
        encoding="utf-8",
    )
    (profiles_dir / "alpha.yml").write_text("models: {}\n", encoding="utf-8")

    from gc_tui.app import TuiApp

    app = TuiApp(settings=tui_settings({"profile": base_profile}))
    app._config_path = str(tmp_path / "base" / "config.yml")
    if session is not None:
        app._session_overrides.update(session)
    return app


def test_profile_picker_options_none_first_marks(tmp_path, monkeypatch):
    """«— без профиля» первым; профили по алфавиту с источником; конфиг-указатель —
    «(как в конфиге)» в скобках без разделителя; активность — ОТДЕЛЬНЫЙ 3-й
    элемент (статус-колонка пикера), не часть описания (решение смока 2026-08-10)."""
    from gc_tui.toggles import profile_picker_options

    app = _picker_app(tmp_path, monkeypatch, base_profile="smoke")
    options = profile_picker_options(app)
    assert options[0][0] == "без профиля"
    assert [name for name, _, _ in options[1:]] == ["alpha", "smoke"]
    by_name = {name: (desc, status) for name, desc, status in options}
    assert "глобальный" in by_name["alpha"][0]
    assert by_name["smoke"][0] == "глобальный (как в конфиге)"
    assert by_name["smoke"][1] == "активен"
    assert by_name["alpha"][1] == ""
    assert by_name["без профиля"][1] == ""


def test_profile_picker_options_none_marked_active_when_no_profile(
    tmp_path, monkeypatch
):
    from gc_tui.toggles import profile_picker_options

    app = _picker_app(tmp_path, monkeypatch)
    options = profile_picker_options(app)
    assert options[0][0] == "без профиля" and options[0][2] == "активен"


def test_profile_picker_options_exactly_one_active_row(tmp_path, monkeypatch):
    """Инвариант: активной может быть РОВНО одна строка — либо «без профиля»,
    либо один профиль, никогда ни одна и никогда две сразу."""
    from gc_tui.toggles import profile_picker_options

    for i, base_profile in enumerate(("", "smoke", "alpha")):
        case_dir = tmp_path / f"case{i}"
        case_dir.mkdir()
        app = _picker_app(case_dir, monkeypatch, base_profile=base_profile)
        options = profile_picker_options(app)
        active = [name for name, _, status in options if status]
        assert len(active) == 1, (base_profile, options)


def test_profile_picker_hint_shows_target_slots_not_effective(
    tmp_path, monkeypatch
):
    """Хинт ховера — слоты ЦЕЛЕВОГО профиля из его файла (выбор ещё не применён),
    не слоты эффективного конфига."""
    from gc_tui.toggles import profile_picker_hint

    app = _picker_app(tmp_path, monkeypatch)  # эффективный конфиг: слотов нет
    hint = profile_picker_hint(app, "smoke")
    assert "default gc" in hint and "глобальный" in hint


def test_profile_picker_hint_survives_an_undecodable_profile(
    tmp_path, monkeypatch
):
    """Файл профиля в чужой кодировке не роняет палитру.

    `UnicodeDecodeError` — подкласс `ValueError`, а не `OSError`, и мимо
    перехвата `(OSError, yaml.YAMLError)` он уходил наружу: стрелка вниз по
    списку профилей валила приложение трейсбеком (живой смок). Профиль при этом
    даже не выбран — хинт строится на одном лишь наведении курсора.
    """
    from gc_tui.toggles import profile_picker_hint

    app = _picker_app(tmp_path, monkeypatch)
    bad = tmp_path / "base" / "profiles" / "enc.yml"
    bad.write_bytes("slots:\n  default: gc\n".encode("utf-16"))

    hint = profile_picker_hint(app, "enc")
    assert isinstance(hint, str)  # хинт без состава, а не крах


def test_profile_picker_hint_none_value(tmp_path, monkeypatch):
    from gc_tui.toggles import profile_picker_hint

    app = _picker_app(tmp_path, monkeypatch)
    assert profile_picker_hint(app, "без профиля") == (
        "настройки берутся только из config.yml — файл профиля не подмешивается"
    )


def test_profiles_picker_opens_on_the_active_row(tmp_path, monkeypatch):
    """Пикер `/profile` открывается на строке «активен»: действующий
    профиль, без профиля — «без профиля» (смок, раунд 2, п. 7)."""
    pytest.importorskip("textual")
    from gc_tui.commands import build_default_registry
    from gc_tui.toggles import profile_picker_options
    from gc_tui.widgets.arg_picker import ArgPicker

    cmd = build_default_registry().lookup("profile")
    for i, base_profile in enumerate(("smoke", "")):
        case_dir = tmp_path / f"case{i}"
        case_dir.mkdir()
        app = _picker_app(case_dir, monkeypatch, base_profile=base_profile)
        current = cmd.arg_current_provider(app)
        picker = ArgPicker(
            list(profile_picker_options(app)), initial_value=current
        )
        assert picker.selected_value() == (base_profile or "без профиля")


def test_profiles_command_has_dynamic_picker_wiring():
    from gc_tui.commands import build_default_registry

    cmd = build_default_registry().lookup("profile")
    assert cmd.args == "[профиль]"
    assert cmd.arg_options_provider is not None
    assert cmd.arg_hint_provider is not None


@pytest.mark.asyncio
async def test_arg_picker_dynamic_footer_hint_follows_selection():
    pytest.importorskip("textual")
    from gc_tui.widgets.arg_picker import ArgPicker
    from textual.app import App

    hints = {
        "без профиля": "чистый конфиг",
        "smoke": "default gc · глобальный",
    }
    picker = ArgPicker(
        [("без профиля", "работать чистым конфигом"), ("smoke", "глобальный")],
        hint_provider=lambda v: hints.get(v, ""),
    )

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


def test_arg_picker_paints_a_refusal_status_as_an_error():
    """Статус-колонка красится по СМЫСЛУ статуса: «✗ не применится» зелёным цветом
    достижения читалось бы как «всё в порядке». Правило — по глифу отказа, а не
    по литералу: пикер не знает ни про профили, ни про их тексты."""
    from gc_tui import palette as p
    from gc_tui.widgets.arg_picker import ArgPicker

    picker = ArgPicker(
        [
            ("normal", "глобальный", "активен"),
            ("broken", "глобальный", "✗ не применится"),
        ]
    )

    spans = {}
    for opt in picker._options:
        row = picker._format_row(opt)
        spans[opt[0]] = [
            row.plain[s.start : s.end] for s in row.spans if s.style == p.ERR
        ]
    assert spans["broken"] == ["✗ не применится"]
    assert spans["normal"] == []


def _bare_trace_screen(monkeypatch, app):
    from gc_tui.screens.main import TraceScreen

    scr = TraceScreen()
    monkeypatch.setattr(
        type(scr), "app", property(lambda self: app), raising=False
    )
    return scr


def test_cmd_profile_dash_removes_session_profile(tmp_path, monkeypatch):
    app = _picker_app(tmp_path, monkeypatch, session={"profile": "smoke"})
    calls = []
    app.switch_session_profile = lambda name: calls.append(name)
    scr = _bare_trace_screen(monkeypatch, app)
    scr._cmd_profile("без профиля")
    assert calls == [""]


def test_cmd_profile_known_name_switches(tmp_path, monkeypatch):
    app = _picker_app(tmp_path, monkeypatch)
    calls = []
    app.switch_session_profile = lambda name: calls.append(name)
    scr = _bare_trace_screen(monkeypatch, app)
    scr._cmd_profile("smoke")
    assert calls == ["smoke"]


def test_cmd_profile_unknown_name_honest_refusal(tmp_path, monkeypatch):
    app = _picker_app(tmp_path, monkeypatch)
    calls, hints = [], []
    app.switch_session_profile = lambda name: calls.append(name)
    scr = _bare_trace_screen(monkeypatch, app)
    scr._show_footer_hint = lambda text, **kw: hints.append(text)
    scr._cmd_profile("wrok")
    assert calls == []
    assert hints and "wrok" in hints[0] and "smoke" in hints[0]


def _raiser(exc):
    def _fn(_name):
        raise exc

    return _fn


def test_cmd_profile_write_failure_shows_hint_not_crash(tmp_path, monkeypatch):
    """Каталог тредов недоступен: `apply_session_override` откатывает и ре-райзит
    OSError. Из обработчика клавиш исключение уронило бы приложение — команда
    показывает то же «✗ не сохранено», что и палитра."""
    app = _picker_app(tmp_path, monkeypatch)
    hints = []
    app.switch_session_profile = _raiser(OSError("read-only"))
    scr = _bare_trace_screen(monkeypatch, app)
    scr._show_footer_hint = lambda text, **kw: hints.append(text)
    scr._cmd_profile("smoke")
    assert hints and "не сохранено" in hints[0] and "read-only" in hints[0]


def test_refusal_hint_outlives_the_ephemeral_default(tmp_path, monkeypatch):
    """Отказ с причиной держится 5с (`_show_footer_refusal`): 2-секундного
    дефолта эфемерных подсказок не хватает прочитать координату и причину
    («slots.default: алиас "ghost" не найден…») — живой смок владельца
    2026-09-04."""
    app = _picker_app(tmp_path, monkeypatch)
    seen = []
    app.switch_session_profile = _raiser(ValueError("slots.default: алиас"))
    scr = _bare_trace_screen(monkeypatch, app)
    scr._show_footer_hint = lambda text, **kw: seen.append(
        (text, kw.get("seconds"))
    )
    scr._cmd_profile("smoke")
    assert seen and "не сохранено" in seen[0][0]
    assert seen[0][1] == 5.0


def test_cmd_profile_dash_write_failure_shows_hint_not_crash(
    tmp_path, monkeypatch
):
    app = _picker_app(tmp_path, monkeypatch, session={"profile": "smoke"})
    hints = []
    app.switch_session_profile = _raiser(OSError("disk full"))
    scr = _bare_trace_screen(monkeypatch, app)
    scr._show_footer_hint = lambda text, **kw: hints.append(text)
    scr._cmd_profile("без профиля")
    assert hints and "не сохранено" in hints[0]


def test_cmd_profile_vanished_profile_shows_hint_not_crash(
    tmp_path, monkeypatch
):
    """Файл профиля исчез между листингом и выбором — пере-мерж внутри
    `switch_session_profile` бросает ValueError."""
    app = _picker_app(tmp_path, monkeypatch)
    hints = []
    app.switch_session_profile = _raiser(ValueError("профиль не найден"))
    scr = _bare_trace_screen(monkeypatch, app)
    scr._show_footer_hint = lambda text, **kw: hints.append(text)
    scr._cmd_profile("smoke")
    assert hints and "не сохранено" in hints[0]


def test_cmd_profile_no_arg_opens_picker(tmp_path, monkeypatch):
    app = _picker_app(tmp_path, monkeypatch)
    opened = []
    scr = _bare_trace_screen(monkeypatch, app)
    scr._open_command_picker = lambda name: opened.append(name)
    scr._cmd_profile("")
    assert opened == ["profile"]


def _add_unassemblable_profile(tmp_path) -> None:
    """К стенду `_picker_app` — профиль в UTF-16: в списке он есть, а собраться
    с ним конфиг не может (файл не читается)."""
    (tmp_path / "base" / "profiles" / "broken-enc.yml").write_text(
        """models: {}
""",
        encoding="utf-16",
    )


def test_cmd_profile_refuses_a_profile_that_does_not_assemble(
    tmp_path, monkeypatch
):
    """Вход 1 (набранная команда), НАСТОЯЩИЙ `switch_session_profile`, не мок:
    профиль, с которым конфиг не собирается, отклоняется на месте — тем же
    «✗ не сохранено», что и сорвавшийся персист. Путь файла в отказе остаётся:
    рядом с палитрой его, в отличие от раздела «Профили» в настройках, никто не
    печатает — без него человеку нечего чинить."""
    app = _picker_app(tmp_path, monkeypatch)
    _add_unassemblable_profile(tmp_path)
    hints = []
    scr = _bare_trace_screen(monkeypatch, app)
    scr._show_footer_hint = lambda text, **kw: hints.append(text)

    scr._cmd_profile("broken-enc")

    assert hints and "не сохранено" in hints[0]
    assert "broken-enc.yml" in hints[0]
    assert "profile" not in app._session_overrides


def test_apply_toggle_profiles_propagates_the_refusal(tmp_path, monkeypatch):
    """Вход 2 (палитра): у `/profile` ←/→ и Enter раскрывают пикер, а его выбор
    уходит в тот же `_cmd_profile` — но generic-путь `apply_toggle` остаётся
    объявленным API тоггла. Он обязан ПРОПУСКАТЬ отказ наружу: оба обработчика в
    `main.py` ловят `(ValueError, OSError)` и показывают «✗ не сохранено»;
    проглоченное здесь исключение вернуло бы тихий фоллбэк через чёрный ход."""
    from gc_tui.toggles import apply_toggle

    app = _picker_app(tmp_path, monkeypatch)
    _add_unassemblable_profile(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        apply_toggle(app, "profiles", "broken-enc")

    assert "broken-enc.yml" in str(excinfo.value)
    assert "profile" not in app._session_overrides


def _add_unresolvable_slot_profile(tmp_path) -> None:
    """Второй способ сломать профиль — не файлом, а СБОРКОЙ: файл читается, YAML
    валиден, но `slots.default` зовёт алиас, которого нет в merged-реестре
    моделей. Витрина обязана судить сборкой, а не читаемостью файла."""
    (tmp_path / "base" / "profiles" / "broken-slot.yml").write_text(
        "slots:\n  default: nope\n", encoding="utf-8"
    )


def test_profile_picker_marks_a_profile_that_does_not_assemble(
    tmp_path, monkeypatch
):
    """Профиль, который гейт выбора отклонит, помечен статусом ДО нажатия Enter:
    отказ после нажатия — половина дела (решение владельца по живому смоку)."""
    from gc_tui.toggles import profile_picker_options

    app = _picker_app(tmp_path, monkeypatch)
    _add_unassemblable_profile(tmp_path)

    by_name = {name: status for name, _, status in profile_picker_options(app)}
    assert by_name["broken-enc"] == "✗ не применится"


def test_profile_picker_marks_a_profile_whose_slot_does_not_resolve(
    tmp_path, monkeypatch
):
    """Тот же статус там, где ломается не чтение файла, а сборка стека: судья —
    полная сборка конфига, а не «читается ли файл» (иначе половина негодных
    профилей проходила бы витрину молча)."""
    from gc_tui.toggles import profile_picker_options

    app = _picker_app(tmp_path, monkeypatch)
    _add_unresolvable_slot_profile(tmp_path)

    by_name = {name: status for name, _, status in profile_picker_options(app)}
    assert by_name["broken-slot"] == "✗ не применится"


def test_profile_picker_leaves_healthy_profiles_without_a_status(
    tmp_path, monkeypatch
):
    """Сторож: рядом с негодным соседом здоровые профили остаются без статуса.
    Пометка «всех подряд» (например, когда не собирается сама база) обесценила бы
    метку — по ней перестали бы отличать негодный профиль от годного."""
    from gc_tui.toggles import profile_picker_options

    app = _picker_app(tmp_path, monkeypatch)
    _add_unassemblable_profile(tmp_path)
    _add_unresolvable_slot_profile(tmp_path)

    by_name = {name: status for name, _, status in profile_picker_options(app)}
    assert by_name["alpha"] == "" and by_name["smoke"] == ""
    assert by_name["без профиля"] == "активен"


def test_profile_picker_keeps_active_marker_on_a_broken_active_profile(
    tmp_path, monkeypatch
):
    """Действующий профиль помечен «активен», даже если его файл испортили под
    ногами: он УЖЕ применён и работает, а «не применится» звало бы человека
    чинить выбор, который никуда не переключается. Два статуса в одной колонке
    не показываем — приоритет у того, что описывает текущее состояние."""
    from gc_tui.toggles import profile_picker_options

    app = _picker_app(tmp_path, monkeypatch, base_profile="broken-enc")
    _add_unassemblable_profile(tmp_path)

    by_name = {name: status for name, _, status in profile_picker_options(app)}
    assert by_name["broken-enc"] == "активен"


def test_profile_picker_hint_names_the_reason_a_profile_will_not_apply(
    tmp_path, monkeypatch
):
    """Статус говорит ЧТО, подсказка — ПОЧЕМУ: без причины человеку нечего чинить
    (текст ядра самодостаточен — «<путь>: не удалось прочитать: …»)."""
    from gc_tui.toggles import profile_picker_hint

    app = _picker_app(tmp_path, monkeypatch)
    _add_unassemblable_profile(tmp_path)

    hint = profile_picker_hint(app, "broken-enc")
    assert "broken-enc.yml" in hint and "не удалось прочитать" in hint


def test_profile_picker_hint_reuses_the_opening_probe(tmp_path, monkeypatch):
    """Открытие пикера (`profile_picker_options`) уже пробует сборку каждого
    непроверенного профиля — хинт подвала (`profile_picker_hint`, зовётся на
    каждый highlight И на каждый тик поллинга `ArgPicker.desired_height`, 0.25 с)
    не имеет права звать вторую полную сборку конфига (`probe_profile_assembly` →
    `load_settings`, ~6 мс на профиль) для профиля, который уже пробовала
    витрина списка."""
    from gc_tui.toggles import profile_picker_hint, profile_picker_options

    app = _picker_app(tmp_path, monkeypatch)
    _add_unassemblable_profile(
        tmp_path
    )  # alpha, smoke — здоровые; broken-enc — нет

    calls: list = []
    original = app.probe_profile_assembly

    def counted(name):
        calls.append(name)
        return original(name)

    monkeypatch.setattr(app, "probe_profile_assembly", counted)

    options = profile_picker_options(app)  # «открытие» пикера
    probed_on_open = list(calls)
    assert probed_on_open, "открытие обязано пробовать хотя бы один профиль"
    calls.clear()

    names = [name for name, _, _ in options if name != "без профиля"]
    for name in names:  # «наведение» — хинт каждой строки
        profile_picker_hint(app, name)
    assert calls == [], (
        f"хинт перепробовал профили, уже проверенные открытием: {calls}"
    )


def test_profile_picker_hint_probes_once_across_repeated_polling(
    tmp_path, monkeypatch
):
    """Хинт одного и того же (ещё не пробованного открытием) профиля, спрошенный
    повторно — как это делает поллинг `ArgPicker.desired_height` каждые 0.25 с,
    пока список просто открыт, — не должен звать пробу заново."""
    from gc_tui.toggles import profile_picker_hint

    app = _picker_app(
        tmp_path, monkeypatch, base_profile="smoke"
    )  # smoke — эффективный,
    calls: list = []  # options() его не трогает
    original = app.probe_profile_assembly

    def counted(name):
        calls.append(name)
        return original(name)

    monkeypatch.setattr(app, "probe_profile_assembly", counted)

    for _ in range(4):
        profile_picker_hint(app, "smoke")
    assert calls == ["smoke"], (
        f"повторный хинт того же профиля пробует заново: {calls}"
    )


def test_session_removal_overrides_base_pointer(tmp_path, monkeypatch):
    """§9.1 ревизия: сессионное "" при конфиг-указателе — чистый конфиг
    (пере-мерж БЕЗ профиля), а не база с профилем."""
    monkeypatch.chdir(tmp_path)
    base_dir = tmp_path / "base"
    (base_dir / "profiles").mkdir(parents=True)
    (base_dir / "profiles" / "work.yml").write_text(
        # token: "" — правило пары: свой адрес идёт со своим токеном (сервер ключа не требует).
        'models:\n  flash:\n    url: http://x\n    token: ""\nslots:\n  default: flash\n',
        encoding="utf-8",
    )
    (base_dir / "config.yml").write_text(
        "profile: work\nbackend:\n  completions_url: http://b\n",
        encoding="utf-8",
    )

    from gc_core.config import load_settings
    from gc_tui.app import TuiApp

    cfg = load_settings(str(base_dir / "config.yml"))
    assert cfg.settings.models  # профиль подмешан

    app = TuiApp(settings=cfg)
    app._config_path = str(base_dir / "config.yml")
    app.set_session_override("profile", "")
    eff = app.effective_settings()
    assert not eff.settings.models  # оверлей профиля снят
    assert eff.runtime.active_profile == ""


# --- Ручной уровень закрепляется на ход; AUTO не шлёт deferred notice ----


def _level_app(tmp_path):
    from gc_tui.app import TuiApp

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("model: default\n", encoding="utf-8")
    app = TuiApp(settings=tui_settings({"model": "default"}))
    app._config_path = str(cfg_file)
    app._threads_dir = str(tmp_path / "threads")
    app.tui_state.active_task_id = "t1"
    return app


def _app_with_live_run(tmp_path):
    app = _level_app(tmp_path)

    class _LiveRunner:
        def is_running(self):
            return True

    app.active_runner = _LiveRunner()
    return app


@pytest.mark.parametrize("running", [True, False, None])
def test_apply_model_choice_emits_no_deferred_notice(tmp_path, running):
    """В ходе AUTO нет ручного пина: переключение не выдаёт notice ручного уровня."""
    pytest.importorskip("textual")
    from gc_core.runtime_notices import (
        clear_runtime_notices,
        collect_runtime_notices,
    )

    clear_runtime_notices()
    app = _app_with_live_run(tmp_path)
    app.active_runner.call_level = lambda: None
    app.auto_mode_active = lambda: True
    if running is None:
        app.active_runner = None
    else:
        app.active_runner.is_running = lambda: running
    app.apply_session_override("model", "max")
    assert app.effective_settings().settings.model == "max"
    assert "level_switch_deferred" not in {
        n.code for n in collect_runtime_notices()
    }


# --- Значения правой колонки считаются один раз на перерисовку -------------


@pytest.mark.asyncio
async def test_palette_rebuild_asks_each_command_value_once(
    tmp_path, monkeypatch
):
    """Перерисовка списка (стрелка, символ фильтра): значение `/model`
    (роутер витрины) и `/profile` считается один раз на рендер, а не на
    каждую строку, которой нужна ширина правой колонки."""
    pytest.importorskip("textual")
    from gc_tui.commands import build_default_registry

    app = _registry_app(tmp_path, monkeypatch)
    reg = build_default_registry()
    calls = {"model": 0, "profile": 0}

    def counting(cmd):
        calls[cmd.name] = calls.get(cmd.name, 0) + 1
        return cmd.value_provider(app) if cmd.value_provider else None

    from gc_tui.widgets.command_palette import CommandPalette

    async with app.run_test(size=(120, 40)) as pilot:
        pal = CommandPalette(reg, initial_filter="", command_value=counting)
        await app.screen.mount(pal)
        await pilot.pause()
        calls.update(model=0, profile=0)

        pal.move_selection(1)

        assert calls["model"] == 1 and calls["profile"] == 1


def test_apply_model_choice_has_no_end_of_task_notice_while_run_is_live(
    tmp_path,
):
    pytest.importorskip("textual")
    from gc_core.runtime_notices import (
        clear_runtime_notices,
        collect_runtime_notices,
    )

    clear_runtime_notices()
    app = _level_app(tmp_path)

    class _LiveRunner:
        def is_running(self):
            return True

    app.active_runner = _LiveRunner()
    app.apply_model_choice("max")

    assert (
        app.effective_settings().settings.model == "max"
    )  # применилось сразу (§ выбор)
    codes = [n.code for n in collect_runtime_notices()]
    assert "level_switch_deferred" not in codes


def test_apply_model_choice_no_notice_without_active_run(tmp_path):
    pytest.importorskip("textual")
    from gc_core.runtime_notices import (
        clear_runtime_notices,
        collect_runtime_notices,
    )

    clear_runtime_notices()
    app = _level_app(tmp_path)
    app.apply_model_choice("max")

    codes = [n.code for n in collect_runtime_notices()]
    assert "level_switch_deferred" not in codes


def test_apply_model_choice_no_notice_after_run_finished(tmp_path):
    pytest.importorskip("textual")
    from gc_core.runtime_notices import (
        clear_runtime_notices,
        collect_runtime_notices,
    )

    clear_runtime_notices()
    app = _level_app(tmp_path)

    class _FinishedRunner:
        def is_running(self):
            return False

    app.active_runner = _FinishedRunner()
    app.apply_model_choice("max")

    codes = [n.code for n in collect_runtime_notices()]
    assert "level_switch_deferred" not in codes
