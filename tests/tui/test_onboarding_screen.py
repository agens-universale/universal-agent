import pytest


def _screen(**over):
    from tui.screens.onboarding import OnboardingScreen
    cfg = {"completions_url": "http://your-backend:PORT/v1/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer <ВПИШИТЕ_ТОКЕН>"},
           "model": "<ВПИШИТЕ_МОДЕЛЬ>"}
    kw = dict(config=cfg, config_path="cfg.yml", review_all=False,
              on_commit=lambda: None, fetch=lambda *a, **k: None,
              mcp=_FakeMcp())
    kw.update(over)
    return OnboardingScreen(**kw)


class _FakeMcp:
    def installable_names(self): return ["duckduckgo", "office"]
    def wired_names(self): return []
    def descriptions(self): return {}
    def install(self, name=None): return 0


def _write_conn_cfg(tmp_path, *, url=None, auth=None, proxy=None, model=None):
    """Настоящий config.yml из шаблона (гарантированно валиден) + write_values по
    dotted-путям. Шаг «Подключение» берёт поля ИЗ ФАЙЛА (сырыми, без резолва
    ${ENV:...}) — на мок-пути ссылку было бы не увидеть."""
    from pathlib import Path
    import onboarding_config_writer as w
    from config import _default_config_path, validate_config_text
    template = Path(_default_config_path())
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    values = {}
    if url is not None:
        values["backend.completions_url"] = url
    if auth is not None:
        values["backend.chat_completion_headers.Authorization"] = auth
    if proxy is not None:
        values["backend.proxy"] = proxy
    if model is not None:
        values["agent.model"] = model
    if values:
        w.write_values(cfg_path, values, template_path=template, validate=validate_config_text)
    return cfg_path


def _resolved_conn_cfg(cfg_path):
    """Конфиг, как его видит приложение: файл + резолв ${ENV:VAR} (тот же
    resolve_env_placeholders, что и config.load_config)."""
    import yaml
    from config_modules.env_interpolation import resolve_env_placeholders
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    backend = raw.get("backend") or {}
    return resolve_env_placeholders({
        "completions_url": backend.get("completions_url"),
        "chat_completion_headers": backend.get("chat_completion_headers") or {},
        "proxy": backend.get("proxy"),
        "model": (raw.get("agent") or {}).get("model"),
    })


def _profiles_cfg(tmp_path, *names, active=None):
    """Настоящий config.yml + профили в корне «рядом с базой» (`<каталог конфига>/
    profiles`, спека §3.1). Шаг «Профили» появляется от ДИСКАВЕРИ, а не от флага —
    поэтому файлы кладутся на диск, а не подсовываются моком. `active` — указатель
    в самом конфиге (проверки снятия профиля мастером)."""
    import onboarding_config_writer as w
    from config import validate_config_text
    from config_modules.profiles import PROFILE_SKELETON
    cfg_path = _write_conn_cfg(tmp_path)
    root = tmp_path / "profiles"
    root.mkdir(exist_ok=True)
    for name in names:
        (root / f"{name}.yml").write_text(PROFILE_SKELETON, encoding="utf-8")
    if active is not None:
        w.write_values(cfg_path, {"profile": active}, validate=validate_config_text)
    return cfg_path


def test_wizard_steps_are_named_and_do_not_include_profiles_without_them(tmp_path, monkeypatch):
    """Состав шагов считается на входе и адресуется ИМЕНАМИ: профилей на дисках
    нет — шага «Профили» нет, остальные (включая скипаемый шаг уровней) есть всегда.
    Числовые индексы шагов константами больше не являются."""
    from tui.screens.onboarding import (STEP_CONFIRM, STEP_CONNECTION, STEP_LEVELS,
                                        STEP_MCP, STEP_MODEL)
    monkeypatch.chdir(tmp_path)                  # проектный корень профилей — не репозиторий
    cfg_path = _write_conn_cfg(tmp_path)
    scr = _screen(config_path=str(cfg_path))
    assert scr._steps == (STEP_CONNECTION, STEP_MODEL, STEP_LEVELS, STEP_MCP, STEP_CONFIRM)
    assert scr._step_id() == STEP_CONNECTION
    # Хром считает шаги по СОСТАВУ, а не по зашитому числу: счётчик в шапке, состав
    # степпера (финала в нём нет) и отметки пройденного на финале.
    assert scr._title_text() == "Настройка · 1/4"
    assert "○ Уровни" in scr._stepper_text().plain
    scr._step = scr._steps.index(STEP_CONFIRM)
    assert scr._title_text() == "Настройка · подтверждение"
    assert scr._stepper_text().plain.count("✓") == 4


def test_wizard_steps_start_with_profiles_when_profiles_exist(tmp_path, monkeypatch):
    """Второй вариант состава: дискавери нашёл профили — первым идёт шаг «Профили»,
    остальные не меняются."""
    from tui.screens.onboarding import (STEP_CONFIRM, STEP_CONNECTION, STEP_LEVELS,
                                        STEP_MCP, STEP_MODEL, STEP_PROFILES)
    monkeypatch.chdir(tmp_path)
    cfg_path = _profiles_cfg(tmp_path, "work", "home")
    scr = _screen(config_path=str(cfg_path))
    assert scr._steps == (STEP_PROFILES, STEP_CONNECTION, STEP_MODEL, STEP_LEVELS,
                          STEP_MCP, STEP_CONFIRM)
    assert scr._step_id() == STEP_PROFILES
    assert scr._title_text() == "Настройка · 1/5"      # шагов на один больше
    assert "● Профили" in scr._stepper_text().plain
    scr._step = scr._steps.index(STEP_CONFIRM)
    assert scr._stepper_text().plain.count("✓") == 5


@pytest.mark.asyncio
async def test_connection_step_mounts_one_editor_with_three_fields(tmp_path):
    """Шаг 0 — одна форма: адрес, токен, прокси."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ids = {i.id for i in ed.query(Input)}
        assert {"conn-url", "conn-token", "conn-proxy"} <= ids


@pytest.mark.asyncio
async def test_connection_step_walks_three_fields_with_arrows(tmp_path):
    """↑/↓ ходят по трём полям формы; строки-галки в create_mode нет.

    Адрес — уже настроенный СВОЙ (не пресет), чтобы пресет-лист не раскрылся сам
    (канон §2.1 ConnectionEditor: раскрывается только на пустом/пресетном поле) и
    не забирал стрелки себе."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    cfg_path = _write_conn_cfg(tmp_path, url="https://custom.example/v1/chat/completions")
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path),
                      config={"completions_url": "https://custom.example/v1/chat/completions",
                              "chat_completion_headers": {}, "model": ""})
        await pilot.app.push_screen(scr)
        await pilot.pause()
        seen = []
        for _ in range(3):
            seen.append(getattr(scr.focused, "id", None))
            await pilot.press("down")
            await pilot.pause()
        assert seen == ["conn-url", "conn-token", "conn-proxy"]


def test_summary_hides_template_sentinels():
    """Шаблонные заглушки конфига (URL/MODEL_SENTINEL) в верхней сводке — прочерк,
    а не «your-backend:PORT» / «<ВПИШИТЕ_МОДЕЛЬ>» как заданное значение."""
    scr = _screen()                       # config = все три ..._SENTINEL
    line = scr._summary_text()
    assert "your-backend" not in line and "ВПИШИТЕ" not in line
    assert "сервер: —" in line and "модель: —" in line


@pytest.mark.asyncio
async def test_connection_address_restored_on_return(tmp_path):
    # ввёл адрес → далее → PgUp назад: значение восстановлено, а не пустое
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()          # список раскрыт по умолчанию на пустом поле
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        await pilot.press("enter")           # → шаг модели
        await pilot.pause()
        assert scr._step == 1
        await pilot.press("pageup")          # PgUp назад на подключение
        await pilot.pause()
        assert scr._step == 0
        assert scr.query_one(ConnectionEditor).query_one("#conn-url", Input).value == \
            "https://h/v1/chat/completions"


@pytest.mark.asyncio
async def test_connection_token_placeholder_shows_masked_on_return(tmp_path):
    """Токен не возвращается в поле открытым текстом (секрет — только маской в
    placeholder, канон ConnectionEditor): после ухода и возврата поле пустое, но
    placeholder напоминает, что токен уже задан."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-token", Input).value = "sk-secret"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._values["Authorization"] == "Bearer sk-secret"
        assert scr._step == 1
        await pilot.press("pageup")
        await pilot.pause()
        assert scr._step == 0
        tok = scr.query_one(ConnectionEditor).query_one("#conn-token", Input)
        assert tok.value == ""                    # секрет не лежит открытым текстом
        assert tok.placeholder == "Bearer sk-***"  # но маска намекает, что он задан


@pytest.mark.asyncio
async def test_connection_token_not_cleared_on_revisit_without_retyping(tmp_path):
    """Токен не трогается пустым полем при повторном проходе шага: секрет никогда
    не лежит в поле открытым текстом (см. тест выше), и если человек вернулся
    поправить адрес и пошёл дальше, не тронув токен, — он не должен потеряться."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-token", Input).value = "sk-secret"
        await pilot.press("enter")             # шаг без пометки — сразу сабмит
        await pilot.pause()
        assert scr._values["Authorization"] == "Bearer sk-secret"
        assert scr._step == 1
        await pilot.press("pageup")             # вернулись поправить адрес
        await pilot.pause()
        assert scr._step == 0
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h2/v1/chat/completions"
        # токен НЕ перепечатан — поле пустое (секрет никогда не показывается открытым текстом)
        assert ed.query_one("#conn-token", Input).value == ""
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step == 1
        assert scr._values["completions_url"] == "https://h2/v1/chat/completions"
        assert scr._values["Authorization"] == "Bearer sk-secret"   # не стёрт пустым полем


async def _final_blob(pilot, scr, *, mcp):
    from tui.screens.onboarding import STEP_CONFIRM
    scr._values = {"completions_url": "https://h/v1", "Authorization": "Bearer sk", "model": "m"}
    scr._mcp_selected = set(mcp)
    scr._goto(STEP_CONFIRM)
    await pilot.pause()
    return " ".join(str(w.render()) for w in scr.query("Static.onb-dyn"))


@pytest.mark.asyncio
async def test_final_summary_shows_mcp_row_none_selected(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        blob = await _final_blob(pilot, scr, mcp=[])
        assert "MCP" in blob                                 # строка MCP есть всегда
        assert "не выбраны" in blob                          # честная фраза «не ставим»
        assert "Установятся после подтверждения" not in blob  # но не обещаем установку


@pytest.mark.asyncio
async def test_final_summary_hides_template_sentinels(tmp_path):
    """Шаблонные плейсхолдеры адреса и модели — не значения (канон витрин: футер,
    /config, строка-сводка мастера). Нетронутое поле в `_values` не попадает
    (`ConnectionEditor.submit()`), а с волны B мастер и `agent.model` не пишет —
    у вернувшегося в мастер человека оба плейсхолдера так и лежат в конфиге, и на
    финале сводка обязана показать прочерк, а не «✓ модель: <ВПИШИТЕ_МОДЕЛЬ>»."""
    pytest.importorskip("textual")
    import onboarding
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_CONFIRM
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()          # config несёт оба сентинела, поля в этот проход не тронуты
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._goto(STEP_CONFIRM)
        await pilot.pause()
        blob = " ".join(str(w.render()) for w in scr.query("Static.onb-dyn"))
        assert onboarding.MODEL_SENTINEL not in blob
        assert onboarding.URL_SENTINEL not in blob
        assert "модель:" in blob and "адрес:" in blob and "—" in blob


@pytest.mark.asyncio
async def test_final_summary_glyph_only_for_filled_rows(tmp_path):
    """✓ — про заполненное: «✓ адрес: —» читается «готово» там, где ничего нет.
    Колонка меток при этом не съезжает (глиф гасится, а не удаляется)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_CONFIRM
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()                    # адрес — плейсхолдер шаблона → «—»
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._values = {"model": "m"}       # …а модель в этот проход выбрана
        scr._goto(STEP_CONFIRM)
        await pilot.pause()
        lines = [ln for w in scr.query("Static.onb-dyn")
                 for ln in str(w.render()).splitlines()]
        addr_line = next(ln for ln in lines if "адрес:" in ln)
        model_line = next(ln for ln in lines if "модель:" in ln)
        assert addr_line.startswith("  ") and "✓" not in addr_line
        assert model_line.startswith("✓ ")
        assert addr_line.index("адрес:") == model_line.index("модель:")


@pytest.mark.asyncio
async def test_final_summary_shows_mcp_when_selected(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        blob = await _final_blob(pilot, scr, mcp=["office"])
        assert "office" in blob
        assert "Установятся после подтверждения: MCP (office)" in blob


@pytest.mark.asyncio
async def test_mcp_installed_marked_and_not_toggleable(tmp_path):
    # уже подключённый MCP помечен «установлен» и не переключается Space
    pytest.importorskip("textual")
    from tui.app import TuiApp

    class _McpInstalled:
        def installable_names(self): return ["office", "science"]
        def wired_names(self): return ["office"]
        def install(self, name=None): return 0

    from tui.screens.onboarding import STEP_MCP
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(mcp=_McpInstalled())
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._goto(STEP_MCP)
        await pilot.pause()
        await pilot.press("space")           # курсор на office (idx 0, установлен) → не выбирается
        await pilot.pause()
        assert "office" not in scr._mcp_selected
        await pilot.press("down")
        await pilot.press("space")           # science → выбирается
        await pilot.pause()
        assert "science" in scr._mcp_selected
        blob = " ".join(str(w.render()) for w in scr.query(".onb-option"))
        assert "подключён" in blob


@pytest.mark.asyncio
async def test_scaffold_shows_logo_stepper_and_esc_exits(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        # логотип и степпер отрисованы
        assert scr.query_one("#onb-logo")
        assert scr.query_one("#onb-stepper")
        assert scr._step == 0
        # Esc → модалка подтверждения (не мгновенный выход); подтверждение → app.exit.
        # Первый Esc на пустом поле адреса сворачивает раскрытый по умолчанию
        # пресет-лист (канон §5.3) — модалка не должна появиться уже на нём.
        exited = []
        app.exit = lambda *a, **k: exited.append(True)
        await pilot.press("escape")
        await pilot.pause()
        from tui.screens.confirm import ConfirmScreen
        assert not isinstance(app.screen, ConfirmScreen)   # первый Esc — только пресеты
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert exited == []
        await pilot.press("y")
        await pilot.pause()
        assert exited == [True]


@pytest.mark.asyncio
async def test_connection_preset_hint_follows_the_cursor_after_esc(tmp_path):
    """Свернув пресет-лист адреса `Esc`, человек должен видеть, чем его вернуть
    (спека 2026-08-06 §4.4) — общий хинт-механизм списков (`show_closed_hint`,
    дословно `LIST_CLOSED_HINT`). До правки `escape_presets` сворачивал список молча.

    Фикс-раунд 1 (ревью): подсказка не переживает уход курсора с адреса — с чужого
    поля `Alt+↓` список адреса уже не раскрывает (`reload_presets` гейтует именно
    позицию курсора). Полный путь: Esc → хинт появился → стрелкой ушли на «Токен» →
    хинта нет → стрелкой вернулись → список раскрылся заново, хинт снова канонный
    (`_PRESET_HINT_EMPTY` — мастер на пустом адресе зовёт выбрать провайдера явно)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import _PRESET_HINT_EMPTY
    from tui.editors.field_list import LIST_CLOSED_HINT
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr.query(".conn-preset-row")     # предусловие: список раскрыт по умолчанию
        await pilot.press("escape")               # свернуть список, шаг мастера жив
        await pilot.pause()
        assert scr._step == 0
        hints = scr.query(".conn-url-preset-hint")
        assert hints and str(hints[-1].render()) == LIST_CLOSED_HINT
        await pilot.press("down")                  # уйти с адреса на токен
        await pilot.pause()
        assert not scr.query(".conn-url-preset-hint")   # подсказка ушла вместе с курсором
        await pilot.press("up")                    # вернуться на адрес
        await pilot.pause()
        assert scr.query(".conn-preset-row")        # список раскрылся заново
        hints = scr.query(".conn-url-preset-hint")
        assert hints and str(hints[-1].render()) == _PRESET_HINT_EMPTY


@pytest.mark.asyncio
async def test_wizard_alt_down_over_own_address_opens_without_a_highlight(tmp_path):
    """Находка A финального ревью 2026-08-06 (парное место мастера): Alt+↓ раскрывает
    пресет-лист поверх своего адреса, которого нет среди пресетов (спека §2.4) —
    строки видны все, подсвеченной нет. Хинт и футер обязаны звать Enter «оставить
    свой адрес», а не «подставить» — то же правило, что в /settings."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from tui.editors.preset_list import PRESET_HINT_NO_MATCH
    from tui.screens.onboarding import _FOOTER_PRESET_LIST_NO_MATCH
    from textual.widgets import Static
    own_url = "https://my-own-host.internal/v1"
    cfg_path = _write_conn_cfg(tmp_path, url=own_url)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path),
                      config={"completions_url": own_url,
                              "chat_completion_headers": {}, "model": ""})
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        assert ed.presets_open is False      # предусловие: свой адрес, список закрыт
        await pilot.press("alt+down")
        await pilot.pause()
        assert ed.presets_open is True
        rows = [str(w.render()) for w in scr.query(".conn-preset-row")]
        assert rows and not any(r.startswith("▸") for r in rows)   # видны все, подсвеченной нет
        hint = str(list(scr.query(".conn-url-preset-hint"))[-1].render())
        assert hint == PRESET_HINT_NO_MATCH
        footer = str(scr.query_one("#onb-footer", Static).render())
        assert footer == _FOOTER_PRESET_LIST_NO_MATCH


@pytest.mark.asyncio
async def test_wizard_connection_footer_names_alt_down_only_on_the_url_field(tmp_path):
    """Находка D финального ревью 2026-08-06 (парное место мастера): футер шага
    «Подключение» обещает Alt+↓, только пока курсор на адресе — на «Токене»/«Прокси»
    клавиша молчит (`ConnectionEditor.on_url_field`)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from textual.widgets import Static
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()                      # адрес пустой — список раскрыт сам
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("escape")          # свернуть список, курсор остаётся на адресе
        await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render())
        assert "Alt+↓ — показать список" in footer
        await pilot.press("down")            # адрес → токен
        await pilot.pause()
        footer2 = str(scr.query_one("#onb-footer", Static).render())
        assert "Alt+↓" not in footer2
        await pilot.press("up")              # обратно на адрес — список раскрылся заново
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        footer3 = str(scr.query_one("#onb-footer", Static).render())
        assert "Alt+↓ — показать список" in footer3


@pytest.mark.asyncio
async def test_connection_invalid_address_shows_error_valid_advances(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        inp = ed.query_one("#conn-url", Input)
        inp.value = "ttp://bad"            # опечатка → невалид
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step == 0               # остались
        assert "http(s)" in str(scr.query_one(".onb-conn-probe").render())  # ошибка в строке-пробе
        inp.value = "https://h/v1/chat/completions"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step == 1               # перешли на модель
        assert scr._values["completions_url"] == "https://h/v1/chat/completions"


@pytest.mark.asyncio
async def test_connection_no_token_writes_empty_auth(tmp_path):
    """create_mode: токен необязателен — пустое поле при первом заходе явно
    пишет «без авторизации», а не оставляет решение на потом."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._values["Authorization"] == ""
        assert scr._step == 1


@pytest.mark.asyncio
async def test_connection_step_advances_with_one_enter_no_mark_needed(tmp_path):
    """Шаг «Подключение» — без пометки: старый двухтакт Enter (сначала пометить
    выбор авторизации, потом «далее») пропал вместе с радио-блоком, одного Enter
    достаточно, если адрес валиден."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-token", Input).value = "tok"
        await pilot.press("enter")            # один Enter — сразу далее
        await pilot.pause()
        assert scr._step == 1


@pytest.mark.asyncio
async def test_connection_token_becomes_bearer_header(tmp_path):
    """Введённый токен уезжает в _values как Bearer-заголовок."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr); await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-token", Input).value = "sk-1"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._values["Authorization"] == "Bearer sk-1"
        assert scr._step == 1


@pytest.mark.asyncio
async def test_model_step_receives_proxy_from_connection_step(tmp_path):
    """Прокси, набранный на шаге «Подключение», обязан доехать до шага «Модель» —
    иначе список моделей уходит запрашивать мимо него: в корп-сети с обязательным
    прокси шаг «Подключение» видит сервер («✓ … · N моделей»), а «Модель» — нет
    («список не получен, впишите вручную»), хотя оба шага смотрят на один сервер."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from tui.editors.model import ModelEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr); await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-proxy", Input).value = "http://corp-proxy:8080"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step == 1
        assert scr.query_one(ModelEditor)._config_proxy == "http://corp-proxy:8080"


@pytest.mark.asyncio
async def test_model_list_marks_and_pick(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from model_picker import ModelInfo
    from tests.tui.conftest import wait_until
    from tui.editors.model import ModelEditor
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    fake = lambda *a, **k: [ModelInfo("Qwen", tool_capable=True),
                            ModelInfo("Flash", tool_capable=False)]
    async with app.run_test() as pilot:
        scr = _screen(fetch=fake)
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._step = 1; scr._render_step()
        # дождаться worker-загрузки в ModelEditor
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor)) and scr.query(ModelEditor).first().models_loaded, "models loaded")
        # ⚠ есть (Flash не tool-capable)
        assert any("⚠" in s.renderable if hasattr(s, "renderable") else "⚠" in str(s.render())
                   for s in scr.query(".onb-option"))
        await pilot.press("enter"); await pilot.pause()   # курсор на Qwen → Enter#1 фиксирует
        assert scr._values["model"] == "Qwen"


@pytest.mark.asyncio
async def test_model_manual_fallback(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from textual.widgets import Input
    from tests.tui.conftest import wait_until
    from tui.editors.model import ModelEditor
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(fetch=lambda *a, **k: None)   # список не получен
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._step = 1; scr._render_step()
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor)) and scr.query(ModelEditor).first().models_loaded, "models loaded")
        scr.query_one("#onb-model", Input).value = "my-model"
        await pilot.press("enter"); await pilot.pause()
        assert scr._values["model"] == "my-model"


@pytest.mark.asyncio
async def test_connection_footer_always_says_next_no_mark_state(tmp_path):
    """Шаг «Подключение» — без пометки ●: футер не переключается на «выбрать» (мёртвая
    механика двухролевого Enter старого шага «Авторизация» сюда не перешла — §5.4 спеки).

    Шаг открывается с раскрытым пресет-листом, и пока он раскрыт, клавиши принадлежат
    ему (Enter подставляет провайдера). «Enter — далее» — это футер шага, и проверяем
    его там, где он и живёт: после того как Esc список свернул."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from textual.widgets import Static
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.pause()
        await pilot.press("escape")                  # свернуть пресет-лист, не выйти
        await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render())
        assert "Enter — далее" in footer
        assert "выбрать" not in footer and "PgDn — далее" not in footer


@pytest.mark.asyncio
async def test_connection_proxy_submitted(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr); await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-proxy", Input).value = "http://proxy:8080"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._values["proxy"] == "http://proxy:8080"
        assert scr._step == 1


@pytest.mark.asyncio
async def test_connection_proxy_restored_on_return(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr); await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-proxy", Input).value = "http://proxy:8080"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step == 1
        await pilot.press("pageup")
        await pilot.pause()
        assert scr._step == 0
        assert scr.query_one(ConnectionEditor).query_one("#conn-proxy", Input).value == \
            "http://proxy:8080"


@pytest.mark.asyncio
async def test_connection_proxy_not_cleared_on_revisit_without_retyping(tmp_path):
    """Тот же класс бага, что у токена (см. test_connection_token_not_cleared_...),
    но на монтировании, не на сабмите: прокси не секрет и не маскируется — он обязан
    лечь в поле готовым значением из файла (review_all), иначе шаг без единого следа
    в UI молча стирает уже настроенный прокси, просто пройдя мимо.

    Нетронутое поле не попадает в _values вовсе (submit() ConnectionEditor: None —
    «не менять», канон /settings) — коммит его и не тронет, файл останется как есть,
    в т.ч. если там лежит ссылка ${ENV:...} (см. test_env_refs_survive_untouched_step)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    cfg_path = _write_conn_cfg(tmp_path, url="https://h/v1/chat/completions",
                               auth="Bearer tok", proxy="http://real-corp-proxy:8080",
                               model="m")
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(review_all=True, config_path=str(cfg_path),
                      config=_resolved_conn_cfg(cfg_path))
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        assert ed.query_one("#conn-proxy", Input).value == "http://real-corp-proxy:8080"
        ed.escape_presets()
        scr._submit_connection()               # поля не тронуты
        assert "proxy" not in scr._values      # нетронуто — писать нечего


@pytest.mark.asyncio
async def test_connection_proxy_explicit_clear_survives_revisit(tmp_path):
    """Парный к тесту выше: если человек ЯВНО стёр прокси (а не просто не тронул
    поле), фолбэк на конфиг не должен вернуть старое значение обратно — иначе
    осознанная очистка отменяется молча. Отличие от «не тронул» — по `is None`
    (ключа в `_values` ещё нет) vs пустая строка (уже стёрли), не по truthy."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    cfg_path = _write_conn_cfg(tmp_path, url="https://h/v1/chat/completions",
                               auth="Bearer tok", proxy="http://OLD-proxy:8080",
                               model="m")
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(review_all=True, config_path=str(cfg_path),
                      config=_resolved_conn_cfg(cfg_path))
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        assert ed.query_one("#conn-proxy", Input).value == "http://OLD-proxy:8080"
        ed.escape_presets()
        ed.query_one("#conn-proxy", Input).value = ""     # явно стёр прокси
        scr._submit_connection()
        assert scr._values["proxy"] == ""
        await pilot.press("pageup")                        # назад на шаг «Подключение»
        await pilot.pause()
        assert scr._step == 0
        assert scr.query_one(ConnectionEditor).query_one("#conn-proxy", Input).value == ""


@pytest.mark.asyncio
async def test_connection_space_in_token_field_types_normally(tmp_path):
    """Пробел в create_mode ничего не «выбирает» (галки «без авторизации» здесь нет,
    в отличие от /settings) — печатается в фокусный Input как обычный символ, в
    отличие от старого шага «Авторизация», где Space вне поля помечал опцию."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr); await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        await pilot.press("down")            # курсор → токен
        await pilot.press("a", "space", "b")
        await pilot.pause()
        assert ed.query_one("#conn-token", Input).value == "a b"
        assert scr._step == 0                # шаг не сдвинулся


@pytest.mark.asyncio
async def test_connection_auto_probe_runs_on_leaving_token_field(tmp_path):
    """Уход с поля токена (стрелкой) запускает авто-пробу; результат ложится в
    строку .onb-conn-probe (ту же, что показывает «— не проверено» до пробы)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from tui.services.settings_probe import ProbeResult
    from textual.widgets import Input
    from tests.tui.conftest import wait_until

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        await pilot.press("down")             # уход с адреса на токен — триггер пробы
        await pilot.pause()
        await wait_until(pilot, lambda: "доступен" in str(scr.query_one(".onb-conn-probe").render()),
                          "проба завершилась")


@pytest.mark.asyncio
async def test_connection_preset_pick_triggers_auto_probe(tmp_path):
    """Подстановка пресета (Enter на открытом списке) — тоже триггер авто-пробы,
    не только уход с поля (спека §5.5)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from tui.services.settings_probe import ProbeResult
    from tests.tui.conftest import wait_until

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        assert ed.presets_open                 # пусто → раскрыт по умолчанию
        await pilot.press("enter")              # подставляет первый пресет
        await pilot.pause()
        assert not ed.presets_open
        await wait_until(pilot, lambda: "доступен" in str(scr.query_one(".onb-conn-probe").render()),
                          "проба завершилась")


@pytest.mark.asyncio
async def test_onboarding_model_step_pgup_goes_back_with_filter(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    from model_picker import ModelInfo
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(fetch=lambda *a, **k: [ModelInfo(id="glm-5.2-fp8", tool_capable=True),
                                             ModelInfo(id="openai/gpt-5.6-terra", tool_capable=True)])
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-token", Input).value = "tok"   # необязателен, но проверяем маршрут
        await pilot.press("pagedown")                   # PgDn — «далее» одним тактом → модель
        await pilot.pause(); await pilot.pause()
        assert scr._step == 1
        scr.query_one("#onb-model", Input).value = "gpt"   # поле само себе фильтр (§3.2.6)
        await pilot.pause()
        scr.query_one("#onb-model", Input).focus()   # фокус в поле (в pilot автофокус после mount не встаёт)
        await pilot.pause()
        assert scr.query_one("#onb-model", Input).has_focus   # PgUp работает, пока фокус в поле
        await pilot.press("pageup")                     # PgUp должен вернуть на шаг подключения
        await pilot.pause()
        assert scr._step == 0


@pytest.mark.asyncio
async def test_onboarding_model_step_filter_and_pick(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    from model_picker import ModelInfo
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(fetch=lambda *a, **k: [ModelInfo(id="glm-5.2-fp8", tool_capable=True),
                                             ModelInfo(id="openai/gpt-5.6-terra", tool_capable=True),
                                             ModelInfo(id="deepseek-v3-base", tool_capable=True)])
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-token", Input).value = "tok"
        await pilot.press("pagedown")                   # PgDn — далее с шага подключения
        await pilot.pause(); await pilot.pause()
        scr.query_one("#onb-model", Input).value = "gpt"   # поле само себе фильтр (§3.2.6)
        await pilot.pause()
        # Фильтр гасит строки через `display`, а не сносит их (общая механика
        # `FieldListController`) — «на экране» и значит `w.display`.
        rows = [str(w.render()) for w in scr.query(".onb-option") if w.display]
        assert any("gpt-5.6-terra" in r for r in rows)              # совпадение осталось
        assert not any("deepseek" in r for r in rows)               # не-совпадение убрано
        # Строка под курсором (glm) фильтром убрана → якорь встаёт на первую строку
        # ВЫБОРА, то есть на уцелевшую модель. Раньше якорем была «вписать вручную»,
        # и до совпавшей модели приходилось доезжать ↑↓ (см. соседний тест на обрывок).
        from tui.editors.model import ModelEditor
        assert scr.query_one(ModelEditor).submit() == "openai/gpt-5.6-terra"
        await pilot.press("enter")                      # Enter#1 — пометить подсвеченную
        await pilot.press("enter")                      # Enter#2 — далее, шаг уровней
        await pilot.pause()
        from tui.screens.onboarding import STEP_LEVELS
        assert scr._step_id() == STEP_LEVELS
        assert scr._values.get("model") == "openai/gpt-5.6-terra"


@pytest.mark.asyncio
async def test_filter_fragment_does_not_become_the_model(tmp_path):
    """Обрывок фильтра не имеет права уехать в конфиг именем модели.

    Курсор стоит на модели, человек печатает обрывок, отфильтровывающий её саму
    («gpt» при выбранной glm). Строка под курсором исчезает — и якорем обязана стать
    первая строка ВЫБОРА, а не «вписать вручную»: на ручной строке submit() отдаёт
    живой текст поля (поле само себе фильтр, §3.2.6), и Enter записал бы в конфиг
    модель «gpt». Путь проверяем настоящий — через двухролевой Enter мастера,
    а не через submit() редактора."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tests.tui.conftest import wait_until
    from textual.widgets import Input
    from model_picker import ModelInfo
    from tui.editors.model import ModelEditor
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config={"completions_url": "https://h/v1/chat/completions",
                              "chat_completion_headers": {}},
                      fetch=lambda *a, **k: [ModelInfo("glm-5.2-fp8", tool_capable=True),
                                             ModelInfo("openai/gpt-5.6-terra", tool_capable=True),
                                             ModelInfo("deepseek-v3-base", tool_capable=True)])
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._step = 1; scr._render_step()
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        await pilot.pause()
        ed = scr.query_one(ModelEditor)
        assert ed.submit() == "glm-5.2-fp8"          # предусловие: курсор на первой модели
        ed.query_one("#onb-model", Input).value = "gpt"   # обрывок: glm уходит, gpt остаётся
        await pilot.pause()
        await pilot.press("enter")                   # Enter#1 — пометить строку под курсором
        await pilot.press("enter")                   # Enter#2 — далее
        await pilot.pause()
        assert scr._values.get("model") != "gpt", "обрывок фильтра записан как модель"
        assert scr._values.get("model") == "openai/gpt-5.6-terra"


# --- контракт интеракции: двухролевой Enter, PgDn-далее, пометка ●, футер ---

@pytest.mark.asyncio
async def test_double_enter_on_invalid_address_keeps_single_probe_line(tmp_path):
    """Повторный Enter на невалидном адресе не роняет приложение: строка-проба —
    одна и та же (пересоздание виджета с тем же классом в одном такте не плодит копий)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "ttp://bad"
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.pause()
        assert app.is_running                     # приложение живо
        assert scr._step == 0
        assert len(scr.query(".onb-conn-probe")) == 1
        await pilot.press("pagedown")             # тот же путь через «далее»
        await pilot.pause()
        assert app.is_running and scr._step == 0
        assert len(scr.query(".onb-conn-probe")) == 1


@pytest.mark.asyncio
async def test_double_enter_on_empty_address_keeps_single_probe_line(tmp_path):
    """То же для пустого (не только невалидного) адреса — двойной Enter не плодит
    вторую строку-пробу и не роняет приложение."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr.query_one(ConnectionEditor).escape_presets()   # список свёрнут — поле по-прежнему пустое
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.pause()
        assert app.is_running
        assert scr._step == 0
        assert len(scr.query(".onb-conn-probe")) == 1
        assert "адрес" in str(scr.query_one(".onb-conn-probe").render())


@pytest.mark.asyncio
async def test_pagedown_advances_connection_step(tmp_path):
    """PgDn — «далее» с любого места шага, включая шаг «Подключение» (фокус в поле)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._step == 1
        assert scr._values["completions_url"] == "https://h/v1/chat/completions"


@pytest.mark.asyncio
async def test_pagedown_keeps_validation_of_connection_step(tmp_path):
    """…с той же валидацией, что Enter-далее: невалидный адрес PgDn не пропускает."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "ttp://bad"
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._step == 0
        assert "http(s)" in str(scr.query_one(".onb-conn-probe").render())


@pytest.mark.asyncio
async def test_pagedown_advances_from_mcp_step(tmp_path):
    """PgDn работает и на шагах без пометки (MCP) — зеркало PgUp. MCP → «Подтверждение»."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_CONFIRM, STEP_MCP
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._goto(STEP_MCP); await pilot.pause()
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._step_id() == STEP_CONFIRM


@pytest.mark.asyncio
async def test_pagedown_does_not_commit_on_confirm_step(tmp_path, monkeypatch):
    """Финал подтверждают Enter'ом: навигационная PgDn конфиг не пишет."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import onboarding_config_writer as w
    from tui.screens.onboarding import STEP_CONFIRM
    written = []
    monkeypatch.setattr(w, "write_values", lambda path, vals, **kw: written.append(vals) or [])
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._goto(STEP_CONFIRM); await pilot.pause()
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._step_id() == STEP_CONFIRM and written == []


@pytest.mark.asyncio
async def test_model_enter_marks_then_advances(tmp_path):
    """Шаг «Модель»: Enter#1 помечает подсвеченную (● переезжает на неё), Enter#2 — далее."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from model_picker import ModelInfo
    from tests.tui.conftest import wait_until
    from tui.editors.model import ModelEditor
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(fetch=lambda *a, **k: [ModelInfo("Qwen", tool_capable=True),
                                             ModelInfo("Flash", tool_capable=True)])
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._step = 1; scr._render_step()
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step == 1
        assert scr._values["model"] == "Qwen"
        assert any("● Qwen" in str(w.render()) for w in scr.query(".onb-option"))
        await pilot.press("enter")
        await pilot.pause()
        from tui.screens.onboarding import STEP_LEVELS
        assert scr._step_id() == STEP_LEVELS


@pytest.mark.asyncio
async def test_model_footer_and_pagedown(tmp_path):
    """Футер шага модели меняется по пометке; PgDn ведёт дальше и без неё."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from model_picker import ModelInfo
    from tests.tui.conftest import wait_until
    from textual.widgets import Static
    from tui.editors.model import ModelEditor
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(fetch=lambda *a, **k: [ModelInfo("Qwen", tool_capable=True)])
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._step = 1; scr._render_step()
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        footer = str(scr.query_one("#onb-footer", Static).render())
        assert "Enter — выбрать" in footer and "PgDn — далее" in footer
        await pilot.press("pagedown")
        await pilot.pause()
        from tui.screens.onboarding import STEP_LEVELS
        assert scr._step_id() == STEP_LEVELS
        assert scr._values["model"] == "Qwen"


@pytest.mark.asyncio
async def test_model_manual_row_two_beat(tmp_path):
    """Своё имя — один такт: строки-посредника больше нет (смок 2026-08-04), поэтому
    метить нечего и Enter сразу ведёт дальше, унося набранное."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from model_picker import ModelInfo
    from tests.tui.conftest import wait_until
    from textual.widgets import Input
    from tui.editors.model import ModelEditor
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(fetch=lambda *a, **k: [ModelInfo("Qwen", tool_capable=True)])
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._step = 1; scr._render_step()
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        # Поле само себе фильтр: вписываем имя, которого нет в списке.
        scr.query_one("#onb-model", Input).value = "my-model"
        await pilot.pause()
        await pilot.press("enter")                 # метить нечего → сразу «далее»
        await pilot.pause()
        from tui.screens.onboarding import STEP_LEVELS
        assert scr._step_id() == STEP_LEVELS and scr._values["model"] == "my-model"


@pytest.mark.asyncio
async def test_model_footer_follows_filter_reselect(tmp_path):
    """Фильтрация двигает курсор (строка исчезла → якорь): роль Enter меняется — футер
    обязан догнать, как на ↑↓."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from model_picker import ModelInfo
    from tests.tui.conftest import wait_until
    from textual.widgets import Input, Static
    from tui.editors.model import ModelEditor
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config={"completions_url": "https://h/v1/chat/completions",
                              "chat_completion_headers": {}, "model": "Qwen"},
                      fetch=lambda *a, **k: [ModelInfo("Qwen", tool_capable=True),
                                             ModelInfo("Flash", tool_capable=True)])
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._step = 1; scr._render_step()
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        await pilot.pause()
        await pilot.press("down")                  # → Flash (не помечена)
        await pilot.pause()
        assert "Enter — выбрать" in str(scr.query_one("#onb-footer", Static).render())
        scr.query_one("#onb-model", Input).value = "qw"   # Flash уходит → курсор на Qwen
        await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render())
        assert "Enter — далее" in footer and "Enter — выбрать" not in footer


@pytest.mark.asyncio
async def test_model_footer_says_next_when_no_list(tmp_path):
    """Сервер не отдал список — остаётся ручной ввод: помечать нечего, Enter ведёт
    дальше, и футер обещает «далее», а не «выбрать»."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tests.tui.conftest import wait_until
    from textual.widgets import Input, Static
    from tui.editors.model import ModelEditor
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(fetch=lambda *a, **k: None)
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._step = 1; scr._render_step()
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render())
        assert "Enter — далее" in footer and "Enter — выбрать" not in footer
        scr.query_one("#onb-model", Input).value = "my-model"
        await pilot.press("enter")
        await pilot.pause()
        from tui.screens.onboarding import STEP_LEVELS
        assert scr._step_id() == STEP_LEVELS and scr._values["model"] == "my-model"


@pytest.mark.asyncio
async def test_model_footer_marked_after_list_loads(tmp_path):
    """Модель уже задана в конфиге: список догрузился, курсор встал на помеченную —
    футер честно обещает «Enter — далее» (список приезжает воркером, футер догоняет)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from model_picker import ModelInfo
    from tests.tui.conftest import wait_until
    from textual.widgets import Static
    from tui.editors.model import ModelEditor
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config={"completions_url": "https://h/v1/chat/completions",
                              "chat_completion_headers": {}, "model": "Qwen"},
                      fetch=lambda *a, **k: [ModelInfo("Qwen", tool_capable=True),
                                             ModelInfo("Flash", tool_capable=True)])
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._step = 1; scr._render_step()
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render())
        assert "Enter — далее" in footer and "Enter — выбрать" not in footer


@pytest.mark.asyncio
async def test_mcp_space_toggles(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_CONFIRM, STEP_MCP
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._goto(STEP_MCP); await pilot.pause()
        await pilot.press("space")          # отметить первый (duckduckgo)
        await pilot.pause()
        assert "duckduckgo" in scr._mcp_selected
        await pilot.press("enter"); await pilot.pause()
        assert scr._step_id() == STEP_CONFIRM


@pytest.mark.asyncio
async def test_summary_line_fills_as_steps_complete(tmp_path):
    """Строка сводки под степпером заполняется по ходу; токен маскируется."""
    from tui.screens.onboarding import OnboardingScreen
    from textual.widgets import Static
    import tui.app as app_mod

    app = app_mod.TuiApp(config={"completions_url": "", "chat_completion_headers": {},
                                 "model": ""})
    async with app.run_test() as pilot:
        scr = OnboardingScreen(config=app._config, config_path=str(tmp_path / "c.yml"),
                               review_all=True, on_commit=lambda: None)
        app.push_screen(scr)
        await pilot.pause()
        scr._values["completions_url"] = "https://46.243.227.147:15000/v1/chat/completions"
        scr._values["Authorization"] = "Bearer sk-SECRET"
        scr._render_step()
        await pilot.pause()
        line = str(scr.query_one("#onb-summary", Static).render())
        assert "46.243.227.147:15000" in line
        assert "sk-SECRET" not in line and "токен: Bearer" in line


@pytest.mark.asyncio
async def test_final_commit_writes_and_calls_on_commit(tmp_path):
    """Финал пишет НАСТОЯЩИЙ файл: подключение — плоскими ключами (решение 10:
    база наследования и живой источник пробы хаба), модель — записью реестра
    `models.<алиас>` + `slots.default`, легаси `agent.model` не трогается.
    Мок `write_values` держал бы только половину пути — вторая идёт мимо него,
    слоем записи реестра."""
    pytest.importorskip("textual")
    import yaml
    import onboarding
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_CONFIRM
    from tests.tui.conftest import wait_until
    cfg_path = _write_conn_cfg(tmp_path)      # чистый config.yml из пакетного шаблона
    committed = []
    installed = []
    mcp = _FakeMcp(); mcp.install = lambda name=None: installed.append(name) or 0
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(on_commit=lambda: committed.append(True), mcp=mcp,
                      config_path=str(cfg_path))
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._values = {"completions_url": "https://h/v1", "Authorization": "Bearer sk", "model": "m"}
        scr._mcp_selected = {"office"}
        scr._goto(STEP_CONFIRM); await pilot.pause()
        await pilot.press("enter")
        await wait_until(pilot, lambda: committed, "commit done")
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert raw["backend"]["completions_url"] == "https://h/v1"
        assert raw["backend"]["chat_completion_headers"]["Authorization"] == "Bearer sk"
        assert raw["models"] == {"m": {"model": "m"}}
        assert raw["slots"]["default"] == "m"
        assert raw["agent"]["model"] == onboarding.MODEL_SENTINEL   # легаси-ключ не тронут
        assert installed == ["office"]
        assert committed == [True]


# --- ссылки на переменные окружения ${ENV:VAR} (настоящий файл + настоящий os.environ) ---
# Критический регресс: шаг «Подключение» показывал/писал РЕЗОЛВНУТОЕ значение
# self._config, а не сырое из файла — непройденный проход мастера подменял ссылку
# ${ENV:...} действующим секретом открытым текстом (или пустой строкой, если
# переменной в этой сессии нет). Идём реальным путём записи (_commit_config →
# файл на диске), а не проверкой _values — тот путь бага не увидел бы.


@pytest.mark.asyncio
async def test_env_refs_survive_untouched_connection_step(tmp_path, monkeypatch):
    """Адрес, токен и прокси — все три ссылки ${ENV:...}, ни одно поле не тронуто:
    после полного прохода шага и коммита файл не изменился ни в одной из трёх строк,
    и ни один резолвнутый секрет в него не попал."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    monkeypatch.setenv("GC_URL", "https://real-server.example/v1/chat/completions")
    monkeypatch.setenv("GC_TOKEN", "sk-REALSECRET123")
    monkeypatch.setenv("GC_PROXY", "http://real-corp-proxy:8080")
    cfg_path = _write_conn_cfg(tmp_path, url="${ENV:GC_URL}",
                               auth="Bearer ${ENV:GC_TOKEN}", proxy="${ENV:GC_PROXY}",
                               model="m")
    original = cfg_path.read_text(encoding="utf-8")
    cfg = _resolved_conn_cfg(cfg_path)
    assert cfg["completions_url"] == "https://real-server.example/v1/chat/completions"
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(review_all=True, config_path=str(cfg_path), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        from tui.editors.connection import ConnectionEditor
        scr.query_one(ConnectionEditor).escape_presets()
        scr._submit_connection()                 # ни одно поле не тронуто
        assert "completions_url" not in scr._values
        assert "Authorization" not in scr._values
        assert "proxy" not in scr._values
        assert scr._commit_config() is True
        content = cfg_path.read_text(encoding="utf-8")
        assert content == original                # файл не тронут вовсе
        assert "sk-REALSECRET123" not in content   # резолвнутый секрет не утёк
        assert "real-server.example" not in content
        assert "${ENV:GC_URL}" in content
        assert "Bearer ${ENV:GC_TOKEN}" in content
        assert "${ENV:GC_PROXY}" in content


@pytest.mark.asyncio
async def test_env_ref_token_survives_when_var_unset(tmp_path, monkeypatch):
    """Ссылка на токен есть, а переменной в этой сессии нет: резолв даёт пустую
    строку (см. resolve_env_placeholders), и прежний код принимал её за «токена не
    было» и честно вписывал «без авторизации» — стирая рабочую ссылку. Ссылка
    обязана остаться, авторизация — не быть стёртой явным "" в файле."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    monkeypatch.delenv("GC_TOKEN", raising=False)
    cfg_path = _write_conn_cfg(tmp_path, url="https://h/v1/chat/completions",
                               auth="Bearer ${ENV:GC_TOKEN}", model="m")
    cfg = _resolved_conn_cfg(cfg_path)
    assert cfg["chat_completion_headers"]["Authorization"] == "Bearer "   # резолв в пустоту
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(review_all=True, config_path=str(cfg_path), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        from tui.editors.connection import ConnectionEditor
        scr.query_one(ConnectionEditor).escape_presets()
        scr._submit_connection()
        assert "Authorization" not in scr._values
        assert scr._commit_config() is True
        content = cfg_path.read_text(encoding="utf-8")
        assert "${ENV:GC_TOKEN}" in content
        assert 'Authorization: ""' not in content


@pytest.mark.asyncio
async def test_connection_new_token_replaces_env_ref(tmp_path, monkeypatch):
    """Человек осознанно вводит новый токен поверх ссылки: записывается введённое
    значение, ссылка ${ENV:...} в файле заменяется — не остаётся молча висеть."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from textual.widgets import Input
    monkeypatch.setenv("GC_TOKEN", "sk-OLDSECRET")
    cfg_path = _write_conn_cfg(tmp_path, url="https://h/v1/chat/completions",
                               auth="Bearer ${ENV:GC_TOKEN}", model="m")
    cfg = _resolved_conn_cfg(cfg_path)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(review_all=True, config_path=str(cfg_path), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-token", Input).value = "sk-typed-by-hand"
        scr._submit_connection()
        assert scr._values["Authorization"] == "Bearer sk-typed-by-hand"
        assert scr._commit_config() is True
        content = cfg_path.read_text(encoding="utf-8")
        assert 'Authorization: "Bearer sk-typed-by-hand"' in content
        assert "${ENV:GC_TOKEN}" not in content


@pytest.mark.asyncio
async def test_env_ref_token_survives_in_auto_mode(tmp_path, monkeypatch):
    """Тот же регресс, что test_env_ref_token_survives_when_var_unset, но на
    код-пути review_all=False — том, по которому мастер идёт, когда запускается
    сам (True только у явного `gc-ua --init-config`). Фолбэк на сырое значение файла
    раньше был под гейтом self._review_all — на этом входе поле токена не
    префилилось и не доразличалось, шаг проходили Enter'ом «как есть», и
    _submit_connection писал в _values честное «без авторизации» поверх реально
    действующей ссылки ${ENV:...}.

    review_all=False здесь выставлен НАПРЯМУЮ, не через is_unconfigured: у
    конфига ниже (url и токен-ссылка заданы, agent.model не тронут — остаётся
    рабочим значением шаблона config.default.yml, а не MODEL_SENTINEL) на самом
    деле закрыты все три требования, реальный auto-запуск на таком файле мастер
    вообще не показал бы. Проверяем сам код-путь review_all=False, а не то, чем
    он в проде включается."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    monkeypatch.setenv("GC_TOKEN", "sk-REALSECRET123")
    cfg_path = _write_conn_cfg(tmp_path, url="https://h/v1/chat/completions",
                               auth="Bearer ${ENV:GC_TOKEN}")   # model не передан — остаётся реальным
                                                                 # значением шаблона, не сентинелом
    cfg = _resolved_conn_cfg(cfg_path)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(review_all=False, config_path=str(cfg_path), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        from tui.editors.connection import ConnectionEditor
        scr.query_one(ConnectionEditor).escape_presets()
        scr._submit_connection()                 # ни одно поле не тронуто — Enter'ом как есть
        assert "Authorization" not in scr._values
        assert scr._commit_config() is True
        content = cfg_path.read_text(encoding="utf-8")
        assert "${ENV:GC_TOKEN}" in content
        assert 'Authorization: ""' not in content


# --- проба шага «Подключение» на конфигах со ссылками ${ENV:...} ---
# Поля теперь показывают сырое значение файла (см. _raw_conn) — на конфиге со
# ссылкой в адресе поле держит буквально "${ENV:GC_URL}", и авто-проба, взятая
# напрямую из поля, раньше стучалась в этот литерал, а не в реальный сервер.


@pytest.mark.asyncio
async def test_auto_probe_resolves_env_ref_in_address(tmp_path, monkeypatch):
    """Проба должна проверять то, что реально уйдёт в запрос, — раскрытый адрес,
    а не литерал ${ENV:...}, которым теперь префилено поле."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.services.settings_probe import ProbeResult
    from tests.tui.conftest import wait_until
    monkeypatch.setenv("GC_URL", "https://real-server.example/v1/chat/completions")
    cfg_path = _write_conn_cfg(tmp_path, url="${ENV:GC_URL}")
    cfg = _resolved_conn_cfg(cfg_path)
    seen = {}

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        seen["endpoint"] = endpoint
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path), config=cfg, probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._auto_probe()
        await wait_until(pilot, lambda: "endpoint" in seen, "проба ушла")
        assert seen["endpoint"] == "https://real-server.example/v1/chat/completions"


@pytest.mark.asyncio
async def test_auto_probe_resolution_does_not_leak_into_field_or_file(tmp_path, monkeypatch):
    """Парный к тесту выше: раскрытие адреса для пробы — только для самого
    запроса, оно не оседает ни в поле, ни в _values, ни в файле."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from tui.services.settings_probe import ProbeResult
    from tests.tui.conftest import wait_until
    from textual.widgets import Input
    monkeypatch.setenv("GC_URL", "https://real-server.example/v1/chat/completions")
    cfg_path = _write_conn_cfg(tmp_path, url="${ENV:GC_URL}")
    cfg = _resolved_conn_cfg(cfg_path)
    seen = {}

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        seen["endpoint"] = endpoint
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path), config=cfg, probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._auto_probe()
        await wait_until(pilot, lambda: "endpoint" in seen, "проба ушла")
        ed = scr.query_one(ConnectionEditor)
        assert ed.query_one("#conn-url", Input).value == "${ENV:GC_URL}"
        ed.escape_presets()
        scr._submit_connection()                 # поле не тронуто
        assert "completions_url" not in scr._values
        assert scr._commit_config() is True
        content = cfg_path.read_text(encoding="utf-8")
        assert "${ENV:GC_URL}" in content
        assert "real-server.example" not in content


# --- проба шага «Подключение» без токена в поле (секрет живёт только под маской) ---
# Поле токена принципиально пустое на нетронутом шаге (секрет никогда не лежит в
# поле открытым текстом) — авто-проба обязана фолбэчиться на действующий токен из
# конфига, как это уже делает соседний шаг «Модель» (_mount_step_body, шаг 1).


@pytest.mark.asyncio
async def test_auto_probe_uses_effective_token_when_field_untouched(tmp_path):
    """Токен в файле — литерал, поле не тронуто: проба обязана уйти с ДЕЙСТВУЮЩИМ
    токеном, а не без авторизации — иначе рабочий сервер, требующий Bearer, отвечает
    401 «токен отклонён» под полностью рабочим конфигом."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.services.settings_probe import ProbeResult
    from tests.tui.conftest import wait_until
    cfg_path = _write_conn_cfg(tmp_path, url="https://h/v1/chat/completions",
                               auth="Bearer sk-REALSECRET123")
    cfg = _resolved_conn_cfg(cfg_path)
    seen = {}

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        seen["auth"] = auth
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path), config=cfg, probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._auto_probe()
        await wait_until(pilot, lambda: "auth" in seen, "проба ушла")
        assert seen["auth"] == "Bearer sk-REALSECRET123"


@pytest.mark.asyncio
async def test_auto_probe_resolves_env_ref_token_when_field_untouched(tmp_path, monkeypatch):
    """То же, но токен в файле записан ссылкой ${ENV:...}: до пробы он доезжает уже
    раскрытым, потому что фолбэк берёт его из self._config (тот резолвится при
    загрузке). Тест сторожит именно это — что ссылка в файле не мешает пробе уйти
    с настоящим секретом. Раскрытие ссылки, НАБРАННОЙ на самом шаге, не покрыто:
    поле токена здесь принципиально пустое, и такой путь сейчас недостижим."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.services.settings_probe import ProbeResult
    from tests.tui.conftest import wait_until
    monkeypatch.setenv("GC_TOKEN", "sk-REALSECRET123")
    cfg_path = _write_conn_cfg(tmp_path, url="https://h/v1/chat/completions",
                               auth="Bearer ${ENV:GC_TOKEN}")
    cfg = _resolved_conn_cfg(cfg_path)
    seen = {}

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        seen["auth"] = auth
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path), config=cfg, probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._auto_probe()
        await wait_until(pilot, lambda: "auth" in seen, "проба ушла")
        assert seen["auth"] == "Bearer sk-REALSECRET123"


# --- гейт «пустой адрес» на ссылке ${ENV:...}, у которой нет переменной ---
# Гейт судил СЫРОЕ значение поля: непустая ссылка ${ENV:GC_URL} его проходит, но
# резолв даёт "" (переменной нет), и проба реально стучалась в пустой адрес — под
# визуально заполненным полем показывалось обманчивое «нет соединения (пустой
# адрес)», как будто мастер сломан.


@pytest.mark.asyncio
async def test_auto_probe_reports_missing_env_var_instead_of_probing(tmp_path, monkeypatch):
    """Переменной в этой сессии нет: проба не должна стартовать вовсе, статус —
    честный про переменную, а не про «пустой адрес»/«нет соединения»."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    monkeypatch.delenv("GC_URL", raising=False)
    cfg_path = _write_conn_cfg(tmp_path, url="${ENV:GC_URL}")
    cfg = _resolved_conn_cfg(cfg_path)
    called = {"n": 0}

    def fake_probe(*a, **k):
        called["n"] += 1
        from tui.services.settings_probe import ProbeResult
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path), config=cfg, probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._auto_probe()
        await pilot.pause()
        assert called["n"] == 0                    # проба не ушла
        line = str(scr.query_one(".onb-conn-probe").render())
        assert "GC_URL" in line
        assert "переменной нет" in line
        assert "пустой адрес" not in line
        assert "нет соединения" not in line


async def test_ctrl_space_reprobes_the_connection_step(tmp_path):
    """Ctrl+Space на поле «Подключения» шлёт пробу — и шлёт её ПОВТОРНО, хотя поля не
    менялись: снимок против дребезга гасит только автопробу, ручную глушить нельзя
    (спека §5.2 канона; развод клавиш — спека 2026-08-06 §2)."""
    pytest.importorskip("textual")
    from textual.widgets import Input
    from tests.tui.conftest import wait_until
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from tui.services.settings_probe import ProbeResult

    calls = []

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        calls.append(endpoint)
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        await pilot.pause()
        assert not calls                       # печать сама по себе пробу не шлёт
        # Развод клавиш (спека 2026-08-06 §2): Ctrl+Space больше не спрашивает список —
        # одно нажатие сразу уходит в пробу.
        await pilot.press("ctrl+space")
        await wait_until(pilot, lambda: len(calls) == 1, "ручная проверка ушла")
        await pilot.press("ctrl+space")        # поля не трогали
        await wait_until(pilot, lambda: len(calls) == 2, "снимок не глушит ручную")


@pytest.mark.asyncio
async def test_alt_down_on_the_model_step_reloads_the_list(tmp_path):
    """Шаг «Модель» мастера — то же поле со списком с сервера, что и секция «Модель»
    в /settings (§9.1 канона): Alt+↓ идёт за списком заново (спека 2026-08-06 §2 —
    Ctrl+Space на этом шаге пробы не даёт, см. `test_ctrl_space_on_the_model_step_does_nothing`).
    Сервер лежал (одинокое поле), ожил — список собирается."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tests.tui.conftest import wait_until
    from model_picker import ModelInfo
    from tui.editors.model import ModelEditor

    calls = []

    def fetch(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return None                                   # сервер лежал
        return [ModelInfo("glm-5.2-fp8", tool_capable=True)]

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config={"completions_url": "https://h/v1/chat/completions",
                              "chat_completion_headers": {}},
                      fetch=fetch)
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._step = 1; scr._render_step()
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        assert not scr.query(".onb-option"), "предусловие: сервер списка не отдал"
        await pilot.press("alt+down")
        await wait_until(pilot, lambda: any("glm-5.2-fp8" in str(w.render())
                                            for w in scr.query(".onb-option")),
                         "список перечитан с сервера")


@pytest.mark.asyncio
async def test_ctrl_space_on_the_model_step_does_nothing(tmp_path):
    """Пробы на шаге «Модель» нет (§2.5, §5.1 спеки 2026-08-06) — Ctrl+Space
    съедается молча: список не трогает, шаг не листает, не падает."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tests.tui.conftest import wait_until
    from model_picker import ModelInfo
    from tui.editors.model import ModelEditor

    def fetch(*a, **k):
        return [ModelInfo("glm-5.2-fp8", tool_capable=True)]

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config={"completions_url": "https://h/v1/chat/completions",
                              "chat_completion_headers": {}},
                      fetch=fetch)
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._step = 1; scr._render_step()
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        rows_before = [str(w.render()) for w in scr.query(".onb-option")]
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert app.screen is scr
        assert scr._step == 1
        assert [str(w.render()) for w in scr.query(".onb-option")] == rows_before


@pytest.mark.asyncio
async def test_ctrl_space_on_an_empty_address_says_what_is_missing(tmp_path):
    """Молчать в ответ на клавишу нельзя (канон draft-probe /settings, где тот же
    случай прямо оговорён комментарием). На чистой установке шаг «Подключение»
    стартует с ПУСТЫМ адресом — именно там новичок жмёт клавишу первым делом, а
    _auto_probe выходит на `if not url` до записи любой строки."""
    pytest.importorskip("textual")
    from textual.widgets import Input
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from tui.services.settings_probe import ProbeResult

    calls = []

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        calls.append(endpoint)
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        await pilot.pause()
        assert not ed.query_one("#conn-url", Input).value   # предусловие: адрес пуст
        # Развод клавиш (спека 2026-08-06 §2): Ctrl+Space сразу про пробу.
        await pilot.press("ctrl+space")
        await pilot.pause()
        line = " ".join(str(w.render()) for w in scr.query(".onb-conn-probe"))
        assert "сначала настройте адрес" in line
        assert not calls, "стучаться в пустой адрес нечем"


@pytest.mark.asyncio
async def test_ctrl_space_on_an_env_reference_still_probes(tmp_path, monkeypatch):
    """Регресс ревью (фикс-раунд 1): шаг «Подключение» держит СЫРОЕ значение файла
    (`_write_conn_cfg`, без резолва) — `${ENV:GC_URL}` там штатное состояние, не
    фильтр. §2.3 бракует её так же, как «gr» (`_valid_endpoint_url` требует http(s)),
    но блокировать пробу здесь нельзя: корп-конфиг на переменных окружения — рабочий
    сценарий, а не край. Ctrl+Space обязан довести до `_auto_probe`, реальным
    нажатием (не прямым вызовом)."""
    pytest.importorskip("textual")
    from textual.widgets import Input
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from tui.services.settings_probe import ProbeResult

    monkeypatch.setenv("GC_URL", "https://real-server.example/v1/chat/completions")
    cfg_path = _write_conn_cfg(tmp_path, url="${ENV:GC_URL}", model="m")
    cfg = _resolved_conn_cfg(cfg_path)
    calls = []

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        calls.append(endpoint)
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path), config=cfg, probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        await pilot.pause()
        assert ed.query_one("#conn-url", Input).value == "${ENV:GC_URL}"   # предусловие
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert calls, "ссылка обязана дойти до пробы, а не быть принятой за фильтр"
        line = " ".join(str(w.render()) for w in scr.query(".onb-conn-probe"))
        assert "сначала выберите адрес" not in line


@pytest.mark.asyncio
async def test_alt_down_on_the_model_step_goes_past_the_session_cache(tmp_path):
    """Мастер оборачивает свой fetch в тот же сессионный кэш (`OnboardingScreen.__init__`),
    значит и здесь перечитывание (Alt+↓) после УДАЧНОЙ загрузки обязано идти мимо
    него — иначе клавиша детерминированный no-op."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tests.tui.conftest import wait_until
    from model_picker import ModelInfo
    from tui.editors.model import ModelEditor

    calls = []

    def fetch(*a, **k):
        calls.append(1)
        base = [ModelInfo("glm-5.2-fp8", tool_capable=True)]
        if len(calls) == 1:
            return base                                # что сервер отдавал сначала
        return base + [ModelInfo("deepseek-v3-base", tool_capable=True)]

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config={"completions_url": "https://h/v1/chat/completions",
                              "chat_completion_headers": {}},
                      fetch=fetch)
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._step = 1; scr._render_step()
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        rows = lambda: [str(w.render()) for w in scr.query(".onb-option")]
        assert any("glm-5.2-fp8" in r for r in rows())          # предусловие
        assert not any("deepseek" in r for r in rows())
        await pilot.press("alt+down")
        await wait_until(pilot, lambda: any("deepseek" in r for r in rows()),
                         "перечитывание должно идти мимо сессионного кэша")


# --- шаг «Профили» (а) и шаг уровней (в) — волна B, Task 8 -------------------
# Оба шага живут в СОСТАВЕ шагов, а не поверх него: состав считается на входе, и
# сравнения кода идут по именам (`_step_id`), а не по номерам.


def _profiles_screen(tmp_path, **over):
    """Мастер на конфиге, рядом с которым лежат профили `home` и `work`."""
    cfg_path = _profiles_cfg(tmp_path, "home", "work")
    return cfg_path, _screen(config_path=str(cfg_path), **over)


@pytest.mark.asyncio
async def test_profiles_step_choose_active_writes_pointer_and_jumps_to_confirm(
        tmp_path, monkeypatch):
    """«Выбрать активный» → под-список профилей → выбор ведёт СРАЗУ на подтверждение
    (мастер не выходит), а указатель уезжает в файл тем же путём записи, что и всё
    остальное, — на подтверждении."""
    pytest.importorskip("textual")
    import yaml
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_CONFIRM, STEP_PROFILES
    from tests.tui.conftest import wait_until
    monkeypatch.chdir(tmp_path)
    committed = []
    cfg_path, scr = _profiles_screen(tmp_path, on_commit=lambda: committed.append(True))
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr._step_id() == STEP_PROFILES
        blob = " ".join(str(w.render()) for w in scr.query(".onb-dyn"))
        assert "home" in blob and "work" in blob        # найденные профили названы
        await pilot.press("enter")                       # «Выбрать активный»
        await pilot.pause()
        assert scr._profile_stage == "pick"
        await pilot.press("enter")                       # первый в списке — home
        await pilot.pause()
        assert scr._step_id() == STEP_CONFIRM
        assert scr._values["profile"] == "home"
        assert yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["profile"] == ""
        await pilot.press("enter")                       # подтвердить и завершить
        await wait_until(pilot, lambda: committed, "commit done")
        assert yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["profile"] == "home"


@pytest.mark.asyncio
async def test_pgup_from_confirm_returns_into_the_wizard(tmp_path, monkeypatch):
    """Прыжок на финал — не выход: PgUp возвращает в мастер существующей механикой
    (шаг назад по составу)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_CONFIRM, STEP_MCP
    monkeypatch.chdir(tmp_path)
    _cfg_path, scr = _profiles_screen(tmp_path)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")                       # «Выбрать активный»
        await pilot.press("enter")                       # профиль → финал
        await pilot.pause()
        assert scr._step_id() == STEP_CONFIRM
        await pilot.press("pageup")
        await pilot.pause()
        assert scr._step_id() == STEP_MCP


@pytest.mark.asyncio
async def test_profiles_step_reconfigure_continues_the_usual_flow(tmp_path, monkeypatch):
    """«Настроить заново» — обычный поток: следующий шаг «Подключение», а указатель
    профиля снимается (решение владельца; сквозной путь до файла — в
    `test_profiles_reconfigure_removes_the_active_profile`)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_CONNECTION
    monkeypatch.chdir(tmp_path)
    _cfg_path, scr = _profiles_screen(tmp_path)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("down")                        # → «Настроить заново»
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step_id() == STEP_CONNECTION
        assert scr._values["profile"] == ""


@pytest.mark.asyncio
async def test_profiles_step_skip_continues_the_usual_flow(tmp_path, monkeypatch):
    """«Пропустить» — обычный поток (план волны B, Task 8): указатель профиля не
    трогается, мастер идёт своими шагами дальше. От «Настроить заново» вариант
    отличается намерением, а не механикой, — оба ведут на «Подключение»."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_CONNECTION
    monkeypatch.chdir(tmp_path)
    _cfg_path, scr = _profiles_screen(tmp_path)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")                        # → «Пропустить»
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step_id() == STEP_CONNECTION
        assert "profile" not in scr._values


@pytest.mark.asyncio
async def test_profile_picker_escape_returns_to_the_options(tmp_path, monkeypatch):
    """Esc-вложенность: под-список профилей — своё под-состояние, Esc сворачивает
    ЕГО, а не выходит из мастера."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.confirm import ConfirmScreen
    monkeypatch.chdir(tmp_path)
    _cfg_path, scr = _profiles_screen(tmp_path)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")                       # «Выбрать активный»
        await pilot.pause()
        assert scr._profile_stage == "pick"
        await pilot.press("escape")
        await pilot.pause()
        assert scr._profile_stage == "menu"
        assert not isinstance(app.screen, ConfirmScreen)
        await pilot.press("escape")                      # а вот теперь — выход из мастера
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)


@pytest.mark.asyncio
async def test_profiles_footer_promises_choose_not_next(tmp_path, monkeypatch):
    """Enter на шаге «Профили» ВЫБИРАЕТ вариант, а не ведёт далее (контракт п.8
    плана) — футер обязан говорить именно это; «далее» здесь уводит PgDn."""
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tui.app import TuiApp
    monkeypatch.chdir(tmp_path)
    _cfg_path, scr = _profiles_screen(tmp_path)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render())
        assert "Enter — выбрать" in footer and "Enter — далее" not in footer
        assert "PgDn — далее" in footer


@pytest.mark.asyncio
async def test_overriding_layer_note_survives_on_the_profiles_step(tmp_path, monkeypatch):
    """Нота о перекрывающем слое (`overriding_layer_note`) висит над телом шага —
    новый первый шаг её не отменяет."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_PROFILES
    monkeypatch.chdir(tmp_path)
    cfg_path = _profiles_cfg(tmp_path, "work")
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path),
                      config={"completions_url": "", "chat_completion_headers": {},
                              "model": "", "profile": "work"})
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr._step_id() == STEP_PROFILES
        note = str(scr.query_one("#onb-profile-warn").render())
        assert "work" in note


# --- шаг уровней (в): две опциональные под-формы ---------------------------


def _levels_screen(tmp_path, **over):
    """Мастер с пройденными шагами подключения и модели, стоящий на шаге уровней."""
    from model_picker import ModelInfo
    cfg_path = _write_conn_cfg(tmp_path)
    kw = dict(config_path=str(cfg_path),
              fetch=lambda *a, **k: [ModelInfo("Flash", tool_capable=True),
                                     ModelInfo("Heavy", tool_capable=True)])
    kw.update(over)
    scr = _screen(**kw)
    scr._values.update({"completions_url": "https://h/v1/chat/completions",
                        "Authorization": "Bearer sk", "model": "Qwen"})
    return cfg_path, scr


@pytest.mark.asyncio
async def test_levels_step_skipped_writes_nothing(tmp_path):
    """Шаг скипаемый: PgDn уводит дальше, ни одна модель уровня не собрана — в файл
    не уезжает ни записи, ни слота."""
    pytest.importorskip("textual")
    import yaml
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_CONFIRM, STEP_LEVELS, STEP_MCP
    from tests.tui.conftest import wait_until
    committed = []
    cfg_path, scr = _levels_screen(tmp_path, on_commit=lambda: committed.append(True))
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._goto(STEP_LEVELS); await pilot.pause()
        assert scr._step_id() == STEP_LEVELS
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._step_id() == STEP_MCP
        assert "model_light" not in scr._values and "model_max" not in scr._values
        scr._goto(STEP_CONFIRM); await pilot.pause()
        await pilot.press("enter")
        await wait_until(pilot, lambda: committed, "commit done")
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert raw["models"] == {"Qwen": {"model": "Qwen"}}
        assert raw["slots"] == {"default": "Qwen"}


@pytest.mark.asyncio
async def test_levels_step_fills_light_and_max(tmp_path):
    """Заполненные уровни — записи реестра + слоты `light`/`max` (та же механика
    выбора, что на шаге «Модель»: Enter#1 помечает, Enter#2 фиксирует)."""
    pytest.importorskip("textual")
    import yaml
    from tui.app import TuiApp
    from tui.editors.model import ModelEditor
    from tui.screens.onboarding import STEP_CONFIRM, STEP_LEVELS
    from tests.tui.conftest import wait_until
    committed = []
    cfg_path, scr = _levels_screen(tmp_path, on_commit=lambda: committed.append(True))
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._goto(STEP_LEVELS); await pilot.pause()
        await pilot.press("enter")                       # под-форма «лёгкая»
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        await pilot.press("enter")                       # Enter#1 — пометить Flash
        await pilot.pause()
        assert scr._values["model_light"] == "Flash"
        await pilot.press("enter")                       # Enter#2 — назад к списку уровней
        await pilot.pause()
        assert scr._levels_stage == "ask"
        await pilot.press("down")                        # → строка «максимальная»
        await pilot.press("enter")
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        await pilot.press("down")                        # → Heavy
        await pilot.press("enter")
        await pilot.pause()
        assert scr._values["model_max"] == "Heavy"
        await pilot.press("enter")
        await pilot.pause()
        scr._goto(STEP_CONFIRM); await pilot.pause()
        await pilot.press("enter")
        await wait_until(pilot, lambda: committed, "commit done")
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert raw["models"] == {"Qwen": {"model": "Qwen"}, "Flash": {"model": "Flash"},
                                 "Heavy": {"model": "Heavy"}}
        assert raw["slots"] == {"default": "Qwen", "light": "Flash", "max": "Heavy"}


@pytest.mark.asyncio
async def test_level_form_escape_returns_to_the_list_without_setting(tmp_path):
    """Esc-вложенность второго под-состояния: под-форма уровня закрывается Esc к
    списку уровней, ничего не зафиксировав."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.model import ModelEditor
    from tui.screens.confirm import ConfirmScreen
    from tui.screens.onboarding import STEP_LEVELS
    from tests.tui.conftest import wait_until
    _cfg_path, scr = _levels_screen(tmp_path)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._goto(STEP_LEVELS); await pilot.pause()
        await pilot.press("enter")
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        await pilot.press("escape")
        await pilot.pause()
        assert scr._levels_stage == "ask"
        assert "model_light" not in scr._values
        assert not isinstance(app.screen, ConfirmScreen)


@pytest.mark.asyncio
async def test_levels_footer_promises_the_keys_that_work(tmp_path):
    """Футер шага: в списке уровней Enter выбирает (далее — PgDn), в под-форме
    работают Alt+↓ (перечитать список) и Esc — назад к списку, а не выход."""
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tui.app import TuiApp
    from tui.editors.model import ModelEditor
    from tui.screens.onboarding import STEP_LEVELS
    from tests.tui.conftest import wait_until
    _cfg_path, scr = _levels_screen(tmp_path)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._goto(STEP_LEVELS); await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render())
        assert "Enter — выбрать" in footer and "PgDn — далее" in footer
        assert "Esc — выход" in footer
        await pilot.press("enter")
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        footer = str(scr.query_one("#onb-footer", Static).render())
        assert "Alt+↓ — перечитать список" in footer
        assert "Esc — назад" in footer and "Esc — выход" not in footer


@pytest.mark.asyncio
async def test_full_pass_through_both_new_steps_writes_valid_config(tmp_path, monkeypatch):
    """Полный проход с обоими новыми шагами: «Настроить заново» → подключение →
    модель → уровень max → MCP → подтверждение. Итог — валидный конфиг (та же
    двухфазная проверка, что у записи реестра)."""
    pytest.importorskip("textual")
    import yaml
    from model_picker import ModelInfo
    from textual.widgets import Input
    from config import validate_config_text_with_sections
    from tui.app import TuiApp
    from tui.editors.connection import ConnectionEditor
    from tui.editors.model import ModelEditor
    from tui.screens.onboarding import STEP_CONFIRM, STEP_LEVELS, STEP_MCP, STEP_MODEL
    from tests.tui.conftest import wait_until
    monkeypatch.chdir(tmp_path)
    committed = []
    cfg_path, scr = _profiles_screen(
        tmp_path, on_commit=lambda: committed.append(True),
        fetch=lambda *a, **k: [ModelInfo("Flash", tool_capable=True),
                               ModelInfo("Heavy", tool_capable=True)])
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("down")                        # «Настроить заново»
        await pilot.press("enter")
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-url", Input).value = "https://h/v1/chat/completions"
        await pilot.press("enter")                       # → шаг «Модель»
        await pilot.pause()
        assert scr._step_id() == STEP_MODEL
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        await pilot.press("enter")                       # пометить Flash
        await pilot.press("enter")                       # → шаг уровней
        await pilot.pause()
        assert scr._step_id() == STEP_LEVELS
        await pilot.press("down")                        # строка «максимальная»
        await pilot.press("enter")
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        await pilot.press("down")                        # → Heavy
        await pilot.press("enter")
        await pilot.press("enter")                       # зафиксировать и вернуться
        await pilot.pause()
        assert scr._step_id() == STEP_LEVELS
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._step_id() == STEP_MCP
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._step_id() == STEP_CONFIRM
        await pilot.press("enter")
        await wait_until(pilot, lambda: committed, "commit done")
        text = cfg_path.read_text(encoding="utf-8")
        validate_config_text_with_sections(text)         # не бросает
        raw = yaml.safe_load(text)
        assert raw["models"] == {"Flash": {"model": "Flash"}, "Heavy": {"model": "Heavy"}}
        assert raw["slots"] == {"default": "Flash", "max": "Heavy"}
        assert raw["profile"] == ""                      # указатель не трогали


# --- находки ревью Task 8 ---


@pytest.mark.asyncio
async def test_profiles_skip_after_a_pick_clears_the_pointer(tmp_path, monkeypatch):
    """Сценарий ревью: выбрал профиль → финал → PgUp обратно на шаг → «Пропустить»
    → подтвердил. Указатель обязан быть СНЯТ: «пропустить» значит «оставить профили
    как есть», а не «записать выбор, о котором человек передумал»."""
    pytest.importorskip("textual")
    import yaml
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_CONFIRM, STEP_PROFILES
    from tests.tui.conftest import wait_until
    monkeypatch.chdir(tmp_path)
    committed = []
    cfg_path, scr = _profiles_screen(tmp_path, on_commit=lambda: committed.append(True))
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")                       # «Выбрать активный»
        await pilot.press("enter")                       # home → финал
        await pilot.pause()
        assert scr._values["profile"] == "home"
        for _ in range(len(scr._steps) - 1):             # PgUp до первого шага
            await pilot.press("pageup")
            await pilot.pause()
        assert scr._step_id() == STEP_PROFILES
        await pilot.press("down")
        await pilot.press("down")                        # → «Пропустить»
        await pilot.press("enter")
        await pilot.pause()
        assert "profile" not in scr._values
        scr._goto(STEP_CONFIRM); await pilot.pause()
        await pilot.press("enter")
        await wait_until(pilot, lambda: committed, "commit done")
        assert yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["profile"] == ""


@pytest.mark.asyncio
async def test_returning_to_the_profiles_step_shows_the_options_again(tmp_path, monkeypatch):
    """Под-состояние шага не переживает уход с него: вернувшийся PgUp'ом человек
    застаёт три варианта, а не под-список профилей, из которого он ушёл."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_PROFILES
    monkeypatch.chdir(tmp_path)
    _cfg_path, scr = _profiles_screen(tmp_path)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")                       # «Выбрать активный»
        await pilot.press("enter")                       # профиль → финал
        await pilot.pause()
        for _ in range(len(scr._steps) - 1):
            await pilot.press("pageup")
            await pilot.pause()
        assert scr._step_id() == STEP_PROFILES
        assert scr._profile_stage == "menu"
        blob = " ".join(str(w.render()) for w in scr.query(".onb-option"))
        assert "Настроить заново" in blob                # варианты, а не имена профилей


@pytest.mark.asyncio
async def test_leaving_the_level_form_does_not_survive_the_step(tmp_path):
    """То же правило для второго под-состояния: под-форма уровня не переживает уход
    со шага — вернувшийся застаёт список уровней."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.editors.model import ModelEditor
    from tui.screens.onboarding import STEP_LEVELS, STEP_MCP
    from tests.tui.conftest import wait_until
    _cfg_path, scr = _levels_screen(tmp_path)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr); await pilot.pause()
        scr._goto(STEP_LEVELS); await pilot.pause()
        await pilot.press("enter")                       # под-форма «лёгкая»
        await wait_until(pilot, lambda: bool(scr.query(ModelEditor))
                         and scr.query(ModelEditor).first().models_loaded, "models loaded")
        scr._goto(STEP_MCP); await pilot.pause()         # ушли со шага мимо под-формы
        await pilot.press("pageup")
        await pilot.pause()
        assert scr._step_id() == STEP_LEVELS
        assert scr._levels_stage == "ask"


# --- решение владельца: три ветки шага «Профили» расходятся по-настоящему ---
# «Выбрать активный» пишет имя · «Настроить заново» СНИМАЕТ профиль (иначе всё,
# что человек настроит следом в основном конфиге, перекроется тем же профилем) ·
# «Пропустить» указателя не касается вовсе.


def _active_profile_screen(tmp_path, **over):
    """Мастер на конфиге с ДЕЙСТВУЮЩИМ профилем `work` (и вторым, `home`, на диске)."""
    cfg_path = _profiles_cfg(tmp_path, "home", "work", active="work")
    kw = dict(config_path=str(cfg_path),
              config={"completions_url": "https://h/v1/chat/completions",
                      "chat_completion_headers": {}, "model": "m", "profile": "work"})
    kw.update(over)
    return cfg_path, _screen(**kw)


@pytest.mark.asyncio
async def test_profiles_reconfigure_removes_the_active_profile(tmp_path, monkeypatch):
    """«Настроить заново» — снять профиль: иначе настройка, ради которой человек
    выбрал этот вариант, легла бы под перекрытие тем же профилем. Финал называет
    снятие, файл после подтверждения несёт пустой указатель."""
    pytest.importorskip("textual")
    import yaml
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_CONFIRM, STEP_CONNECTION
    from tests.tui.conftest import wait_until
    monkeypatch.chdir(tmp_path)
    committed = []
    cfg_path, scr = _active_profile_screen(tmp_path, on_commit=lambda: committed.append(True))
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("down")                        # → «Настроить заново»
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step_id() == STEP_CONNECTION
        assert scr._values["profile"] == ""
        scr._goto(STEP_CONFIRM); await pilot.pause()
        blob = " ".join(str(w.render()) for w in scr.query("Static.onb-dyn"))
        assert "профиль:" in blob and "work" in blob      # снятие названо на подтверждении
        await pilot.press("enter")
        await wait_until(pilot, lambda: committed, "commit done")
        assert yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["profile"] == ""


@pytest.mark.asyncio
async def test_profiles_skip_leaves_the_active_profile_alone(tmp_path, monkeypatch):
    """«Пропустить» — указателя не касается ВОВСЕ: действующий профиль переживает
    мастер, ни записи, ни снятия."""
    pytest.importorskip("textual")
    import yaml
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_CONFIRM
    from tests.tui.conftest import wait_until
    monkeypatch.chdir(tmp_path)
    committed = []
    cfg_path, scr = _active_profile_screen(tmp_path, on_commit=lambda: committed.append(True))
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")                        # → «Пропустить»
        await pilot.press("enter")
        await pilot.pause()
        assert "profile" not in scr._values
        scr._goto(STEP_CONFIRM); await pilot.pause()
        await pilot.press("enter")
        await wait_until(pilot, lambda: committed, "commit done")
        assert yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["profile"] == "work"


@pytest.mark.asyncio
async def test_layer_note_goes_silent_after_reconfigure(tmp_path, monkeypatch):
    """Плашка предупреждает «профиль перекроет ваш выбор». После «Настроить заново»
    профиль будет СНЯТ — перекрывать нечем, и плашка обязана замолчать, иначе
    интерфейс обещает то, чего не произойдёт."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    monkeypatch.chdir(tmp_path)
    _cfg_path, scr = _active_profile_screen(tmp_path)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert 'действует профиль "work"' in str(scr.query_one("#onb-profile-warn").render())
        await pilot.press("down")                        # → «Настроить заново»
        await pilot.press("enter")
        await pilot.pause()
        assert not scr.query("#onb-profile-warn")


@pytest.mark.asyncio
async def test_layer_note_stays_after_skip(tmp_path, monkeypatch):
    """Парный случай: «Пропустить» профиль не трогает — он и дальше перекроет
    настройку, и плашка обязана об этом говорить."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    monkeypatch.chdir(tmp_path)
    _cfg_path, scr = _active_profile_screen(tmp_path)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")                        # → «Пропустить»
        await pilot.press("enter")
        await pilot.pause()
        assert 'действует профиль "work"' in str(scr.query_one("#onb-profile-warn").render())


@pytest.mark.asyncio
async def test_layer_note_appears_after_choosing_a_profile(tmp_path, monkeypatch):
    """Обратное направление того же правила: профиля не было, человек выбрал его на
    первом шаге и вернулся настраивать — теперь его выбор перекроет уже ВЫБРАННЫЙ
    профиль, и плашка обязана появиться."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.onboarding import STEP_CONNECTION
    monkeypatch.chdir(tmp_path)
    _cfg_path, scr = _profiles_screen(tmp_path)          # активного профиля в конфиге нет
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert not scr.query("#onb-profile-warn")        # предупреждать не о чем
        await pilot.press("enter")                       # «Выбрать активный»
        await pilot.press("enter")                       # home → финал
        await pilot.pause()
        while scr._step_id() != STEP_CONNECTION:         # вернулись настраивать
            await pilot.press("pageup")
            await pilot.pause()
        assert 'действует профиль "home"' in str(scr.query_one("#onb-profile-warn").render())
