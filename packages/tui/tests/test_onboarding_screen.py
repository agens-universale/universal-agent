from gc_testkit.settings import make_settings
from gc_testkit.tui_support import as_settings, tui_settings
import pytest

# Хвост подсказки прокрутки зависит от того, влезло ли содержимое в окно
# теста, и к предмету этих сверок не относится.
from gc_tui.form_frame import FOOTER_SCROLL
from form_harness import goto_step


def _screen(**over):
    from gc_tui.screens.onboarding import OnboardingScreen

    cfg = {
        "backend.completions_url": "http://your-backend:PORT/v1/chat/completions",
        "backend.chat_completion_headers": {
            "Authorization": "Bearer <ВПИШИТЕ_ТОКЕН>"
        },
        "agent.model": "<ВПИШИТЕ_МОДЕЛЬ>",
    }
    kw = dict(
        config=cfg,
        config_path="cfg.yml",
        review_all=False,
        on_commit=lambda: None,
        fetch=lambda *a, **k: None,
        mcp=_FakeMcp(),
    )
    kw.update(over)
    if isinstance(kw["config"], dict) and "agent.model" not in kw["config"]:
        # В загруженном конфиге `agent.model` есть всегда; словарь теста без
        # него означает шаблон — плейсхолдер, а не умолчание схемы.
        kw["config"] = {**kw["config"], "agent.model": cfg["agent.model"]}
    kw["settings"] = as_settings(kw.pop("config"))
    return OnboardingScreen(**kw)


@pytest.mark.asyncio
async def test_connection_step_submits_accepted_literal_auth(tmp_path):
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONNECTION
    from gc_tui.services.settings_probe import ProbeResult
    from textual.widgets import Input

    cfg_path = _write_conn_cfg(
        tmp_path, url="https://example.test/chat", auth="secret", model="m"
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            config=_resolved_conn_cfg(cfg_path),
            config_path=str(cfg_path),
            probe=lambda *a, **k: ProbeResult(
                "unknown", None, None, 0, "offline"
            ),
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_CONNECTION)
        await pilot.pause()
        scr._on_auto_probe_done(
            ProbeResult(
                "ok",
                200,
                None,
                1,
                "ok",
                auth_state="accepted",
                accepted_auth="Bearer secret",
            ),
            scr._probe_gen,
        )
        assert scr.query_one("#conn-token", Input).value == "Bearer secret"
        scr._submit_connection()
        assert scr._values["Authorization"] == "Bearer secret"
        assert "Bearer secret" not in cfg_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_connection_edit_invalidates_inflight_probe_before_accepted_auth(
    tmp_path,
):
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONNECTION
    from gc_tui.services.settings_probe import ProbeResult
    from textual.widgets import Input

    cfg_path = _write_conn_cfg(
        tmp_path, url="https://example.test/chat", auth="secret", model="m"
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            config=_resolved_conn_cfg(cfg_path), config_path=str(cfg_path)
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_CONNECTION)
        await pilot.pause()
        stale_gen = scr._probe_gen
        token = scr.query_one("#conn-token", Input)
        token.value = "new-secret"
        await pilot.pause()
        assert scr._probe_gen != stale_gen
        assert scr._last_probed_snapshot is None

        scr._on_auto_probe_done(
            ProbeResult(
                "ok",
                200,
                None,
                1,
                "ok",
                auth_state="accepted",
                accepted_auth="Bearer secret",
            ),
            stale_gen,
        )
        assert token.value == "new-secret"


class _FakeMcp:
    def installable_names(self):
        return ["duckduckgo", "office"]

    def wired_names(self):
        return []

    def descriptions(self):
        return {}

    def install(self, name=None):
        return 0


def _write_conn_cfg(tmp_path, *, url=None, auth=None, proxy=None, model=None):
    """Настоящий config.yml из шаблона (гарантированно валиден) + write_values по
    dotted-путям — тот же приём, что _write_cloud_config в test_onboarding_voice_step.py.
    Шаг «Подключение» берёт поля ИЗ ФАЙЛА (сырыми, без резолва ${ENV:...}) — на
    мок-пути ссылку было бы не увидеть."""
    from pathlib import Path

    from gc_core import onboarding_config_writer as w
    from gc_core.config import _default_config_path, validate_config_text

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
        w.write_values(
            cfg_path,
            values,
            template_path=template,
            validate=validate_config_text,
        )
    return cfg_path


def _resolved_conn_cfg(cfg_path):
    """Конфиг, как его видит приложение: файл + резолв ${ENV:VAR} (тот же
    resolve_env_placeholders, что и config.load_config)."""
    import yaml
    from gc_core.config_modules.env_interpolation import (
        resolve_env_placeholders,
    )

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    backend = raw.get("backend") or {}
    return resolve_env_placeholders(
        {
            "backend.completions_url": backend.get("completions_url"),
            "backend.chat_completion_headers": backend.get(
                "chat_completion_headers"
            )
            or {},
            "backend.proxy": backend.get("proxy"),
            "agent.model": (raw.get("agent") or {}).get("model"),
        }
    )


def _profiles_cfg(tmp_path, *names, active=None):
    """Настоящий config.yml + профили в корне «рядом с базой» (`<каталог конфига>/
    profiles`, спека §3.1). Файлы на диске — обстановка: мастер профилями не
    управляет и от их наличия не зависит вовсе, а тесты обязаны это показывать на
    настоящей раскладке. `active` — указатель в самом конфиге: им и задаётся
    ДЕЙСТВУЮЩИЙ профиль, о котором мастер информирует."""
    from gc_core import onboarding_config_writer as w
    from gc_core.config import validate_config_text
    from gc_core.config_modules.profiles import PROFILE_SKELETON

    cfg_path = _write_conn_cfg(tmp_path)
    root = tmp_path / "profiles"
    root.mkdir(exist_ok=True)
    for name in names:
        (root / f"{name}.yml").write_text(PROFILE_SKELETON, encoding="utf-8")
    if active is not None:
        w.write_values(
            cfg_path, {"profile": active}, validate=validate_config_text
        )
    return cfg_path


def test_wizard_steps_are_named_and_static():
    """Шаги адресуются именами, состав один и тот же всегда (спека §2).

    «Голосовой ввод» стоит между MCP и подтверждением — тем же местом, что и в
    install.html, readme и шапке `/settings`: порядок шагов человек читает раньше,
    чем видит, и расходиться эти два описания не имеют права."""
    from gc_tui.screens.onboarding import (
        _BASE_STEPS,
        STEP_CONFIRM,
        STEP_CONNECTION,
        STEP_LEVELS,
        STEP_MCP,
        STEP_MODEL,
        STEP_VOICE,
    )

    scr = _screen()
    assert (
        scr._steps
        == _BASE_STEPS
        == (
            STEP_CONNECTION,
            STEP_MODEL,
            STEP_LEVELS,
            STEP_MCP,
            STEP_VOICE,
            STEP_CONFIRM,
        )
    )


def test_header_counter_names_the_position_and_the_finale():
    """Шапка называет позицию человека в мастере: «N/5» по всем шагам подряд
    (знаменатель — шаги БЕЗ финального) и «подтверждение» вместо счётчика на
    финале. Сверяется весь список заголовков разом — сдвиг на единицу, лишний
    шаг в знаменателе или счётчик на финале ломают его."""
    from gc_tui.screens.onboarding import STEP_CONFIRM

    scr = _screen()
    titles = []
    for position in range(len(scr._steps)):
        scr._step = position
        titles.append(scr._title_text())
    assert titles == [
        "Настройка · 1/5",
        "Настройка · 2/5",
        "Настройка · 3/5",
        "Настройка · 4/5",
        "Настройка · 5/5",
        "Настройка · подтверждение",
    ]
    assert (
        scr._steps[len(titles) - 1] == STEP_CONFIRM
    )  # без счётчика — именно финал


def test_wizard_steps_are_static_with_profiles_on_disk(tmp_path, monkeypatch):
    """Состав шагов больше не зависит ни от профилей на диске, ни от каталога
    запуска: шага «Профили» нет, дискавери нет вовсе (спека §2, §9)."""
    monkeypatch.chdir(tmp_path)
    from gc_tui.screens.onboarding import _BASE_STEPS

    _cfg_path, scr = _active_profile_screen(tmp_path)
    assert scr._steps == _BASE_STEPS


def test_wizard_module_has_no_profiles_step_left():
    """Негатив на недоснятое: имя шага и его дискавери должны исчезнуть, иначе
    мёртвая ветка тихо переживёт правку (спека §8)."""
    from gc_core import onboarding
    from gc_tui.screens import onboarding as screen_mod

    assert not hasattr(screen_mod, "STEP_PROFILES")
    assert not hasattr(onboarding, "discover_profiles_for_onboarding")


@pytest.mark.asyncio
async def test_connection_step_mounts_one_editor_with_three_fields(tmp_path):
    """Шаг 0 — одна форма: адрес, токен, прокси."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
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
    from gc_tui.app import TuiApp

    cfg_path = _write_conn_cfg(
        tmp_path, url="https://custom.example/v1/chat/completions"
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            config_path=str(cfg_path),
            config={
                "backend.completions_url": "https://custom.example/v1/chat/completions",
                "backend.chat_completion_headers": {},
                "agent.model": "",
            },
        )
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
    scr = _screen()  # config = все три ..._SENTINEL
    line = scr._summary_text()
    assert "your-backend" not in line and "ВПИШИТЕ" not in line
    assert "сервер: —" in line and "модель: —" in line


def test_summary_includes_voice_segment_once_chosen():
    """Верхняя сводка добавляет сегмент «голосовой ввод: …» по мере выбора на шаге
    голоса; пока выбора нет — сегмента нет вовсе (не «голосовой ввод: —»)."""
    scr = _screen()
    assert "голосовой ввод" not in scr._summary_text()
    scr._voice_choice = "local"
    assert "голосовой ввод: локальный сервис" in scr._summary_text()
    scr._voice_choice = "cloud"
    assert "голосовой ввод: свой endpoint" in scr._summary_text()
    scr._voice_choice = "skip"
    assert "голосовой ввод: нет" in scr._summary_text()


@pytest.mark.asyncio
async def test_connection_address_restored_on_return(tmp_path):
    # ввёл адрес → далее → PgUp назад: значение восстановлено, а не пустое
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()  # список раскрыт по умолчанию на пустом поле
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        await pilot.press("enter")  # → шаг модели
        await pilot.pause()
        assert scr._step == 1
        await pilot.press("pageup")  # PgUp назад на подключение
        await pilot.pause()
        assert scr._step == 0
        assert (
            scr.query_one(ConnectionEditor).query_one("#conn-url", Input).value
            == "https://h/v1/chat/completions"
        )


@pytest.mark.asyncio
async def test_connection_token_placeholder_shows_masked_on_return(tmp_path):
    """Токен не возвращается в поле открытым текстом (секрет — только маской в
    placeholder, канон ConnectionEditor): после ухода и возврата поле пустое, но
    placeholder напоминает, что токен уже задан."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-token", Input).value = "sk-secret"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._values["Authorization"] == "sk-secret"
        assert scr._step == 1
        await pilot.press("pageup")
        await pilot.pause()
        assert scr._step == 0
        tok = scr.query_one(ConnectionEditor).query_one("#conn-token", Input)
        assert tok.value == ""  # секрет не лежит открытым текстом
        assert tok.placeholder == "***"  # но маска намекает, что он задан


@pytest.mark.asyncio
async def test_connection_token_not_cleared_on_revisit_without_retyping(
    tmp_path,
):
    """Токен не трогается пустым полем при повторном проходе шага: секрет никогда
    не лежит в поле открытым текстом (см. тест выше), и если человек вернулся
    поправить адрес и пошёл дальше, не тронув токен, — он не должен потеряться."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-token", Input).value = "sk-secret"
        await pilot.press("enter")  # шаг без пометки — сразу сабмит
        await pilot.pause()
        assert scr._values["Authorization"] == "sk-secret"
        assert scr._step == 1
        await pilot.press("pageup")  # вернулись поправить адрес
        await pilot.pause()
        assert scr._step == 0
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h2/v1/chat/completions"
        # токен НЕ перепечатан — поле пустое (секрет никогда не показывается открытым текстом)
        assert ed.query_one("#conn-token", Input).value == ""
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step == 1
        assert (
            scr._values["completions_url"] == "https://h2/v1/chat/completions"
        )
        assert (
            scr._values["Authorization"] == "sk-secret"
        )  # не стёрт пустым полем


async def _final_blob(pilot, scr, *, mcp):
    from gc_tui.screens.onboarding import STEP_CONFIRM

    scr._values = {
        "completions_url": "https://h/v1",
        "Authorization": "Bearer sk",
        "model": "m",
    }
    scr._mcp_selected = set(mcp)
    goto_step(scr, STEP_CONFIRM)
    await pilot.pause()
    return " ".join(str(w.render()) for w in scr.query("Static.onb-dyn"))


@pytest.mark.asyncio
async def test_final_summary_shows_mcp_row_none_selected(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        blob = await _final_blob(pilot, scr, mcp=[])
        assert "MCP" in blob  # строка MCP есть всегда
        assert "не выбраны" in blob  # честная фраза «не ставим»
        assert (
            "Установятся после подтверждения" not in blob
        )  # но не обещаем установку


@pytest.mark.asyncio
async def test_final_summary_hides_template_sentinels(tmp_path):
    """Шаблонные плейсхолдеры адреса и модели — не значения (канон витрин: футер,
    /config, строка-сводка мастера). Нетронутое поле в `_values` не попадает
    (`ConnectionEditor.submit()`), а с волны B мастер и `agent.model` не пишет —
    у вернувшегося в мастер человека оба плейсхолдера так и лежат в конфиге, и на
    финале сводка обязана показать прочерк, а не «✓ модель: <ВПИШИТЕ_МОДЕЛЬ>»."""
    pytest.importorskip("textual")
    from gc_core import onboarding
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = (
            _screen()
        )  # config несёт оба сентинела, поля в этот проход не тронуты
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        blob = " ".join(str(w.render()) for w in scr.query("Static.onb-dyn"))
        assert onboarding.MODEL_SENTINEL not in blob
        assert onboarding.URL_SENTINEL not in blob
        assert "модель default:" in blob and "адрес:" in blob and "—" in blob


@pytest.mark.asyncio
async def test_final_summary_glyph_only_for_filled_rows(tmp_path):
    """✓ — про заполненное: «✓ адрес: —» читается «готово» там, где ничего нет.
    Колонка меток при этом не съезжает (глиф гасится, а не удаляется)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()  # адрес — плейсхолдер шаблона → «—»
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._values = {"model": "m"}  # …а модель в этот проход выбрана
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        lines = [
            ln
            for w in scr.query("Static.onb-dyn")
            for ln in str(w.render()).splitlines()
        ]
        addr_line = next(ln for ln in lines if "адрес:" in ln)
        model_line = next(ln for ln in lines if "модель default:" in ln)
        assert addr_line.startswith("  ") and "✓" not in addr_line
        assert model_line.startswith("✓ ")
        assert addr_line.index("адрес:") == model_line.index("модель default:")


@pytest.mark.asyncio
async def test_final_summary_shows_a_level_assigned_in_the_config(tmp_path):
    """Уровень, заданный в конфиге и не тронутый в ЭТОМ прогоне, обязан быть
    виден на подтверждении — иначе одна и та же модель то есть в сводке, то
    пропадает в зависимости от того, заходил ли человек на её под-форму.
    Строка `default` уже читает резолвнутый конфиг (`_shown_values`) — здесь
    та же дисциплина через `_level_value` (`tui/screens/onboarding.py:287`)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM

    cfg = {
        "backend.completions_url": "https://h/v1/chat/completions",
        "backend.chat_completion_headers": {"Authorization": "Bearer sk"},
        "agent.model": "m",
        "models": {"fast": {"model": "qwen/qwen3-8b"}},
        "slots": {"light": "fast"},
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert (
            "model_light" not in scr._values
        )  # уровень не тронут в этом прогоне
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        lines = [
            ln
            for w in scr.query("Static.onb-dyn")
            for ln in str(w.render()).splitlines()
        ]
        light_lines = [ln for ln in lines if "модель light:" in ln]
        assert light_lines, (
            f"строка уровня light отсутствует в сводке: {lines}"
        )
        assert "qwen/qwen3-8b" in light_lines[0]


@pytest.mark.asyncio
async def test_final_summary_unset_level_says_so_and_gates_the_glyph(tmp_path):
    """Три строки уровней печатаются ВСЕГДА (решение владельца, уравнивает TUI
    с CLI-сводкой): незаданный уровень не пропадает из сводки, а честно
    называется `не задана` — и, как «✓ адрес: —», без галочки."""
    pytest.importorskip("textual")
    from gc_core import onboarding_fields as _of
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()  # реестра нет вовсе — оба уровня не заданы
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        lines = [
            ln
            for w in scr.query("Static.onb-dyn")
            for ln in str(w.render()).splitlines()
        ]
        light_lines = [ln for ln in lines if "модель light:" in ln]
        assert light_lines, (
            f"строка уровня light отсутствует в сводке: {lines}"
        )
        light_line = light_lines[0]
        assert _of.LEVEL_UNSET in light_line
        assert light_line.startswith("  ") and "✓" not in light_line


@pytest.mark.asyncio
async def test_final_summary_shows_mcp_when_selected(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        blob = await _final_blob(pilot, scr, mcp=["office"])
        assert "office" in blob
        assert "Установятся после подтверждения: MCP (office)" in blob


@pytest.mark.asyncio
async def test_final_summary_notice_combines_mcp_and_voice(
    tmp_path, monkeypatch
):
    """Одна служебная строка без скобок объединяет обе части (какие применимы),
    воздух — одна пустая строка после строк сводки."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.voice_runtime import paths as vpaths

    monkeypatch.setattr(
        vpaths, "voice_data_dir", lambda: tmp_path / "voice_data"
    )
    monkeypatch.setattr(
        "gc_tui.voice_runtime.state.voice_state_path",
        lambda: tmp_path / "voice_data" / "state.json",
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._voice_choice = "local"
        blob = await _final_blob(pilot, scr, mcp=["office", "ddgs"])
        assert (
            "Установятся после подтверждения: MCP (ddgs, office) · локальный голосовой сервис (~0.9 ГБ)"
        ) in blob


@pytest.mark.asyncio
async def test_final_summary_notice_voice_only(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.voice_runtime import paths as vpaths

    monkeypatch.setattr(
        vpaths, "voice_data_dir", lambda: tmp_path / "voice_data"
    )
    monkeypatch.setattr(
        "gc_tui.voice_runtime.state.voice_state_path",
        lambda: tmp_path / "voice_data" / "state.json",
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._voice_choice = "local"
        blob = await _final_blob(pilot, scr, mcp=[])
        assert (
            "Установятся после подтверждения: локальный голосовой сервис (~0.9 ГБ)"
            in blob
        )
        assert "MCP (" not in blob


@pytest.mark.asyncio
async def test_mcp_installed_marked_and_not_toggleable(tmp_path):
    # уже подключённый MCP помечен «установлен» и не переключается Space
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _McpInstalled:
        def installable_names(self):
            return ["office", "science"]

        def wired_names(self):
            return ["office"]

        def install(self, name=None):
            return 0

    from gc_tui.screens.onboarding import STEP_MCP

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(mcp=_McpInstalled())
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_MCP)
        await pilot.pause()
        await pilot.press(
            "space"
        )  # курсор на office (idx 0, установлен) → не выбирается
        await pilot.pause()
        assert "office" not in scr._mcp_selected
        await pilot.press("down")
        await pilot.press("space")  # science → выбирается
        await pilot.pause()
        assert "science" in scr._mcp_selected
        blob = " ".join(str(w.render()) for w in scr.query(".onb-option"))
        assert "подключён" in blob


@pytest.mark.asyncio
async def test_scaffold_shows_logo_stepper_and_esc_exits(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
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
        from gc_tui.screens.confirm import ConfirmScreen

        assert not isinstance(
            app.screen, ConfirmScreen
        )  # первый Esc — только пресеты
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
    (спека 2026-08-06 §4.4) — тот же хинт, что у голосовых списков (`show_closed_hint`,
    с клавишей раскрытия мастера). До правки `escape_presets` сворачивал список молча.

    Канон 2026-08-17: строка под адресом ПОСТОЯННА — уход курсора её не снимает.
    Она ярлык способа, а не команда момента (как `Space — переключить` у галки,
    работающая только со своей позиции); что нажимать сейчас, отвечает футер.
    Полный путь: Esc → хинт появился → ушли на «Токен» → хинт НА МЕСТЕ (раньше он
    исчезал) → вернулись на пустой адрес → список поднялся снова, хинт канонный
    (`_PRESET_HINT_EMPTY` — мастер на пустом адресе зовёт выбрать провайдера явно)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import _PRESET_HINT_EMPTY

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr.query(
            ".conn-preset-row"
        )  # предусловие: список раскрыт по умолчанию
        await pilot.press("escape")  # свернуть список, шаг мастера жив
        await pilot.pause()
        assert scr._step == 0
        hints = scr.query(".conn-url-preset-hint")
        assert hints and str(hints[-1].render()) == "Alt+↓ — показать список"
        await pilot.press("down")  # уйти с адреса на токен
        await pilot.pause()
        hints = scr.query(".conn-url-preset-hint")
        assert (
            hints and str(hints[-1].render()) == "Alt+↓ — показать список"
        )  # строка постоянна
        await pilot.press("up")  # вернуться на адрес
        await pilot.pause()
        assert scr.query(".conn-preset-row")  # список раскрылся заново
        hints = scr.query(".conn-url-preset-hint")
        assert hints and str(hints[-1].render()) == _PRESET_HINT_EMPTY


@pytest.mark.asyncio
async def test_custom_connection_url_keeps_onboarding_closed_list_hint(
    tmp_path,
):
    from textual.widgets import Input
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr.query_one(
            "#conn-url", Input
        ).value = "https://custom.example/v1/chat/completions"
        await pilot.pause()
        assert not scr.query(".conn-preset-row")
        hints = scr.query(".conn-url-preset-hint")
        assert (
            str(hints[-1].render())
            == "в списке такого нет · Alt+↓ — показать список"
        )


@pytest.mark.asyncio
async def test_wizard_ctrl_down_opens_the_field_list_before_scrolling(
    tmp_path,
):
    """Ctrl+↓ в мастере — как в /settings: сначала список поля под курсором
    (тихий синоним Alt+↓), прокрутка тела — только если клавишу никто не
    разобрал. Перехват прокруткой в начале on_key отнимал её у списка."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor

    own_url = "https://my-own-host.internal/v1"
    cfg_path = _write_conn_cfg(tmp_path, url=own_url)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            config_path=str(cfg_path),
            config={
                "backend.completions_url": own_url,
                "backend.chat_completion_headers": {},
                "agent.model": "",
            },
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        assert ed.presets_open is False
        await pilot.press("ctrl+down")
        await pilot.pause()
        assert ed.presets_open is True


@pytest.mark.asyncio
async def test_wizard_alt_down_over_own_address_opens_without_a_highlight(
    tmp_path,
):
    """Находка A финального ревью 2026-08-06 (парное место мастера): Alt+↓ раскрывает
    пресет-лист поверх своего адреса, которого нет среди пресетов (спека §2.4) —
    строки видны все, подсвеченной нет. Хинт и футер обязаны звать Enter «оставить
    свой адрес», а не «подставить» — то же правило, что в /settings."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from gc_tui.editors.preset_list import PRESET_HINT_NO_MATCH
    from gc_tui.screens.onboarding import _FOOTER_PRESET_LIST_NO_MATCH
    from textual.widgets import Static

    own_url = "https://my-own-host.internal/v1"
    cfg_path = _write_conn_cfg(tmp_path, url=own_url)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            config_path=str(cfg_path),
            config={
                "backend.completions_url": own_url,
                "backend.chat_completion_headers": {},
                "agent.model": "",
            },
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        assert (
            ed.presets_open is False
        )  # предусловие: свой адрес, список закрыт
        await pilot.press("alt+down")
        await pilot.pause()
        assert ed.presets_open is True
        rows = [str(w.render()) for w in scr.query(".conn-preset-row")]
        assert rows and not any(
            r.startswith("▸") for r in rows
        )  # видны все, подсвеченной нет
        hint = str(list(scr.query(".conn-url-preset-hint"))[-1].render())
        assert hint == PRESET_HINT_NO_MATCH
        footer = str(scr.query_one("#onb-footer", Static).render()).replace(
            FOOTER_SCROLL, ""
        )
        assert footer == _FOOTER_PRESET_LIST_NO_MATCH


@pytest.mark.asyncio
async def test_wizard_connection_footer_names_alt_down_only_on_the_url_field(
    tmp_path,
):
    """Находка D финального ревью 2026-08-06 (парное место мастера): футер шага
    «Подключение» обещает Alt+↓, только пока курсор на адресе — на «Токене»/«Прокси»
    клавиша молчит (`ConnectionEditor.on_url_field`)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()  # адрес пустой — список раскрыт сам
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press(
            "escape"
        )  # свернуть список, курсор остаётся на адресе
        await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render()).replace(
            FOOTER_SCROLL, ""
        )
        assert "Alt+↓ — показать список" in footer
        await pilot.press("down")  # адрес → токен
        await pilot.pause()
        footer2 = str(scr.query_one("#onb-footer", Static).render()).replace(
            FOOTER_SCROLL, ""
        )
        assert "Alt+↓" not in footer2
        await pilot.press("up")  # обратно на адрес — список раскрылся заново
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        footer3 = str(scr.query_one("#onb-footer", Static).render()).replace(
            FOOTER_SCROLL, ""
        )
        assert "Alt+↓ — показать список" in footer3


@pytest.mark.asyncio
async def test_connection_invalid_address_shows_error_valid_advances(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        inp = ed.query_one("#conn-url", Input)
        inp.value = "ttp://bad"  # опечатка → невалид
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step == 0  # остались
        assert "http(s)" in str(
            scr.query_one(".onb-conn-probe").render()
        )  # ошибка в строке-пробе
        inp.value = "https://h/v1/chat/completions"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step == 1  # перешли на модель
        assert (
            scr._values["completions_url"] == "https://h/v1/chat/completions"
        )


@pytest.mark.asyncio
async def test_connection_no_token_writes_empty_auth(tmp_path):
    """create_mode: токен необязателен — пустое поле при первом заходе явно
    пишет «без авторизации», а не оставляет решение на потом."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._values["Authorization"] == ""
        assert scr._step == 1


@pytest.mark.asyncio
async def test_connection_step_advances_with_one_enter_no_mark_needed(
    tmp_path,
):
    """Шаг «Подключение» — без пометки: старый двухтакт Enter (сначала пометить
    выбор авторизации, потом «далее») пропал вместе с радио-блоком, одного Enter
    достаточно, если адрес валиден."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-token", Input).value = "tok"
        await pilot.press("enter")  # один Enter — сразу далее
        await pilot.pause()
        assert scr._step == 1


@pytest.mark.asyncio
async def test_connection_token_saved_as_typed(tmp_path):
    """Введённый токен уезжает в _values as-is (без тихой подстановки Bearer)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-token", Input).value = "sk-1"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._values["Authorization"] == "sk-1"
        assert scr._step == 1


@pytest.mark.asyncio
async def test_model_step_receives_proxy_from_connection_step(tmp_path):
    """Прокси, набранный на шаге «Подключение», обязан доехать до шага «Модель» —
    иначе список моделей уходит запрашивать мимо него: в корп-сети с обязательным
    прокси шаг «Подключение» видит сервер («✓ … · N моделей»), а «Модель» — нет
    («список не получен, впишите вручную»), хотя оба шага смотрят на один сервер."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from gc_tui.editors.model import ModelEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-proxy", Input).value = "http://corp-proxy:8080"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step == 1
        assert (
            scr.query_one(ModelEditor)._config_proxy
            == "http://corp-proxy:8080"
        )


@pytest.mark.asyncio
async def test_model_list_marks_and_pick(tmp_path):
    pytest.importorskip("textual")
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    fake = lambda *a, **k: [
        ModelInfo("Qwen", tool_capable=True),
        ModelInfo("Flash", tool_capable=False),
    ]
    async with app.run_test() as pilot:
        scr = _screen(fetch=fake)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._step = 1
        scr._render_step()
        # дождаться worker-загрузки в ModelEditor
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        # ⚠ есть (Flash не tool-capable)
        assert any(
            "⚠" in s.renderable
            if hasattr(s, "renderable")
            else "⚠" in str(s.render())
            for s in scr.query(".onb-option")
        )
        await pilot.press("enter")
        await pilot.pause()  # курсор на Qwen → Enter#1 фиксирует
        assert scr._values["model"] == "Qwen"


@pytest.mark.asyncio
async def test_model_manual_fallback(tmp_path):
    pytest.importorskip("textual")
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(fetch=lambda *a, **k: None)  # список не получен
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._step = 1
        scr._render_step()
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        scr.query_one("#onb-model", Input).value = "my-model"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._values["model"] == "my-model"


@pytest.mark.asyncio
async def test_connection_footer_always_says_next_no_mark_state(tmp_path):
    """Шаг «Подключение» — без пометки ●: футер не переключается на «выбрать» (мёртвая
    механика двухролевого Enter старого шага «Авторизация» сюда не перешла — §5.4 спеки).

    Шаг открывается с раскрытым пресет-листом, и пока он раскрыт, клавиши принадлежат
    ему (Enter подставляет провайдера). «Enter — далее» — это футер шага, и проверяем
    его там, где он и живёт: после того как Esc список свернул."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.pause()
        await pilot.press("escape")  # свернуть пресет-лист, не выйти
        await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render()).replace(
            FOOTER_SCROLL, ""
        )
        assert "Enter — далее" in footer
        assert "выбрать" not in footer and "PgDn — далее" not in footer


@pytest.mark.asyncio
async def test_connection_proxy_submitted(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-proxy", Input).value = "http://proxy:8080"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._values["proxy"] == "http://proxy:8080"
        assert scr._step == 1


@pytest.mark.asyncio
async def test_connection_proxy_restored_on_return(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-proxy", Input).value = "http://proxy:8080"
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step == 1
        await pilot.press("pageup")
        await pilot.pause()
        assert scr._step == 0
        assert (
            scr.query_one(ConnectionEditor)
            .query_one("#conn-proxy", Input)
            .value
            == "http://proxy:8080"
        )


@pytest.mark.asyncio
async def test_connection_proxy_not_cleared_on_revisit_without_retyping(
    tmp_path,
):
    """Тот же класс бага, что у токена (см. test_connection_token_not_cleared_...),
    но на монтировании, не на сабмите: прокси не секрет и не маскируется — он обязан
    лечь в поле готовым значением из файла (review_all), иначе шаг без единого следа
    в UI молча стирает уже настроенный прокси, просто пройдя мимо.

    Нетронутое поле не попадает в _values вовсе (submit() ConnectionEditor: None —
    «не менять», канон /settings) — коммит его и не тронет, файл останется как есть,
    в т.ч. если там лежит ссылка ${ENV:...} (см. test_env_refs_survive_untouched_step)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    cfg_path = _write_conn_cfg(
        tmp_path,
        url="https://h/v1/chat/completions",
        auth="Bearer tok",
        proxy="http://real-corp-proxy:8080",
        model="m",
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            review_all=True,
            config_path=str(cfg_path),
            config=_resolved_conn_cfg(cfg_path),
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        assert (
            ed.query_one("#conn-proxy", Input).value
            == "http://real-corp-proxy:8080"
        )
        ed.escape_presets()
        scr._submit_connection()  # поля не тронуты
        assert "proxy" not in scr._values  # нетронуто — писать нечего


@pytest.mark.asyncio
async def test_connection_proxy_explicit_clear_survives_revisit(tmp_path):
    """Парный к тесту выше: если человек ЯВНО стёр прокси (а не просто не тронул
    поле), фолбэк на конфиг не должен вернуть старое значение обратно — иначе
    осознанная очистка отменяется молча. Отличие от «не тронул» — по `is None`
    (ключа в `_values` ещё нет) vs пустая строка (уже стёрли), не по truthy."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    cfg_path = _write_conn_cfg(
        tmp_path,
        url="https://h/v1/chat/completions",
        auth="Bearer tok",
        proxy="http://OLD-proxy:8080",
        model="m",
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            review_all=True,
            config_path=str(cfg_path),
            config=_resolved_conn_cfg(cfg_path),
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        assert (
            ed.query_one("#conn-proxy", Input).value == "http://OLD-proxy:8080"
        )
        ed.escape_presets()
        ed.query_one("#conn-proxy", Input).value = ""  # явно стёр прокси
        scr._submit_connection()
        assert scr._values["proxy"] == ""
        await pilot.press("pageup")  # назад на шаг «Подключение»
        await pilot.pause()
        assert scr._step == 0
        assert (
            scr.query_one(ConnectionEditor)
            .query_one("#conn-proxy", Input)
            .value
            == ""
        )


@pytest.mark.asyncio
async def test_connection_space_in_token_field_types_normally(tmp_path):
    """Пробел в create_mode ничего не «выбирает» (галки «без авторизации» здесь нет,
    в отличие от /settings) — печатается в фокусный Input как обычный символ, в
    отличие от старого шага «Авторизация», где Space вне поля помечал опцию."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        await pilot.press("down")  # курсор → токен
        await pilot.press("a", "space", "b")
        await pilot.pause()
        assert ed.query_one("#conn-token", Input).value == "a b"
        assert scr._step == 0  # шаг не сдвинулся


@pytest.mark.asyncio
async def test_connection_auto_probe_runs_on_leaving_token_field(tmp_path):
    """Уход с поля токена (стрелкой) запускает авто-пробу; результат ложится в
    строку .onb-conn-probe (ту же, что показывает «— не проверено» до пробы)."""
    pytest.importorskip("textual")
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from gc_tui.services.settings_probe import ProbeResult
    from textual.widgets import Input

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        await pilot.press("down")  # уход с адреса на токен — триггер пробы
        await pilot.pause()
        # Фейк отвечает и на контрольный запрос без токена → честный вердикт
        # «авторизация не требуется», а не «доступен».
        await wait_until(
            pilot,
            lambda: (
                "не требует" in str(scr.query_one(".onb-conn-probe").render())
                or "не требуется"
                in str(scr.query_one(".onb-conn-probe").render())
            ),
            "проба завершилась",
        )


async def test_connection_auto_probe_preserves_ca_path(tmp_path):
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from gc_tui.services.settings_probe import ProbeResult
    from textual.widgets import Input

    seen = []

    def probe(endpoint, auth, verify, model, *, config_proxy=None):
        seen.append(verify)
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        screen = _screen(
            config={"backend.use_ssl": "/corp/root.pem"}, probe=probe
        )
        await pilot.app.push_screen(screen)
        await pilot.pause()
        editor = screen.query_one(ConnectionEditor)
        editor.escape_presets()
        editor.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        await pilot.press("down")
        await wait_until(
            pilot, lambda: bool(seen), "проба с корпоративным CA выполнена"
        )
        assert set(seen) == {"/corp/root.pem"}


@pytest.mark.asyncio
async def test_connection_preset_pick_triggers_auto_probe(tmp_path):
    """Подстановка пресета (Enter на открытом списке) — тоже триггер авто-пробы,
    не только уход с поля (спека §5.5)."""
    pytest.importorskip("textual")
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from gc_tui.services.settings_probe import ProbeResult

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        assert ed.presets_open  # пусто → раскрыт по умолчанию
        await pilot.press("enter")  # подставляет первый пресет
        await pilot.pause()
        assert not ed.presets_open
        # Фейк отвечает и на контрольный запрос без токена → честный вердикт
        # «авторизация не требуется», а не «доступен»: токен здесь ни на что
        # не влияет, и писать про него что-либо было бы неправдой.
        await wait_until(
            pilot,
            lambda: (
                "не требует" in str(scr.query_one(".onb-conn-probe").render())
                or "не требуется"
                in str(scr.query_one(".onb-conn-probe").render())
            ),
            "проба завершилась",
        )


@pytest.mark.asyncio
async def test_onboarding_model_step_pgup_goes_back_with_filter(tmp_path):
    pytest.importorskip("textual")
    from gc_core.model_picker import ModelInfo
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            fetch=lambda *a, **k: [
                ModelInfo(id="glm-5.2-fp8", tool_capable=True),
                ModelInfo(id="openai/gpt-5.6-terra", tool_capable=True),
            ]
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        ed.query_one(
            "#conn-token", Input
        ).value = "tok"  # необязателен, но проверяем маршрут
        await pilot.press("pagedown")  # PgDn — «далее» одним тактом → модель
        await pilot.pause()
        await pilot.pause()
        assert scr._step == 1
        scr.query_one(
            "#onb-model", Input
        ).value = "gpt"  # поле само себе фильтр (§3.2.6)
        await pilot.pause()
        scr.query_one(
            "#onb-model", Input
        ).focus()  # фокус в поле (в pilot автофокус после mount не встаёт)
        await pilot.pause()
        assert scr.query_one(
            "#onb-model", Input
        ).has_focus  # PgUp работает, пока фокус в поле
        await pilot.press("pageup")  # PgUp должен вернуть на шаг подключения
        await pilot.pause()
        assert scr._step == 0


@pytest.mark.asyncio
async def test_onboarding_model_step_filter_and_pick(tmp_path):
    pytest.importorskip("textual")
    from gc_core.model_picker import ModelInfo
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            fetch=lambda *a, **k: [
                ModelInfo(id="glm-5.2-fp8", tool_capable=True),
                ModelInfo(id="openai/gpt-5.6-terra", tool_capable=True),
                ModelInfo(id="deepseek-v3-base", tool_capable=True),
            ]
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        ed.query_one("#conn-token", Input).value = "tok"
        await pilot.press("pagedown")  # PgDn — далее с шага подключения
        await pilot.pause()
        await pilot.pause()
        scr.query_one(
            "#onb-model", Input
        ).value = "gpt"  # поле само себе фильтр (§3.2.6)
        await pilot.pause()
        # Фильтр гасит строки через `display`, а не сносит их (общая механика
        # `FieldListController`) — «на экране» и значит `w.display`.
        rows = [str(w.render()) for w in scr.query(".onb-option") if w.display]
        assert any("gpt-5.6-terra" in r for r in rows)  # совпадение осталось
        assert not any("deepseek" in r for r in rows)  # не-совпадение убрано
        # Строка под курсором (glm) фильтром убрана → якорь встаёт на первую строку
        # ВЫБОРА, то есть на уцелевшую модель. Раньше якорем была «вписать вручную»,
        # и до совпавшей модели приходилось доезжать ↑↓ (см. соседний тест на обрывок).
        from gc_tui.editors.model import ModelEditor

        model_editor = scr.query_one(ModelEditor)
        assert (
            model_editor._list.selected_item.model.id == "openai/gpt-5.6-terra"
        )
        assert model_editor.submit() == "glm-5.2-fp8"
        await pilot.press("enter")  # Enter#1 — пометить подсвеченную
        assert model_editor.submit() == "openai/gpt-5.6-terra"
        await pilot.press("enter")  # Enter#2 — далее, шаг уровней
        await pilot.pause()
        from gc_tui.screens.onboarding import STEP_LEVELS

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
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            config={
                "backend.completions_url": "https://h/v1/chat/completions",
                "backend.chat_completion_headers": {},
            },
            fetch=lambda *a, **k: [
                ModelInfo("glm-5.2-fp8", tool_capable=True),
                ModelInfo("openai/gpt-5.6-terra", tool_capable=True),
                ModelInfo("deepseek-v3-base", tool_capable=True),
            ],
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._step = 1
        scr._render_step()
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        await pilot.pause()
        ed = scr.query_one(ModelEditor)
        assert (
            ed.submit() == "glm-5.2-fp8"
        )  # предусловие: курсор на первой модели
        ed.query_one(
            "#onb-model", Input
        ).value = "gpt"  # обрывок: glm уходит, gpt остаётся
        await pilot.pause()
        await pilot.press("enter")  # Enter#1 — пометить строку под курсором
        await pilot.press("enter")  # Enter#2 — далее
        await pilot.pause()
        assert scr._values.get("model") != "gpt", (
            "обрывок фильтра записан как модель"
        )
        assert scr._values.get("model") == "openai/gpt-5.6-terra"


# --- контракт интеракции: двухролевой Enter, PgDn-далее, пометка ●, футер ---


@pytest.mark.asyncio
async def test_double_enter_on_invalid_address_keeps_single_probe_line(
    tmp_path,
):
    """Повторный Enter на невалидном адресе не роняет приложение: строка-проба —
    одна и та же (пересоздание виджета с тем же классом в одном такте не плодит копий)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
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
        assert app.is_running  # приложение живо
        assert scr._step == 0
        assert len(scr.query(".onb-conn-probe")) == 1
        await pilot.press("pagedown")  # тот же путь через «далее»
        await pilot.pause()
        assert app.is_running and scr._step == 0
        assert len(scr.query(".onb-conn-probe")) == 1


@pytest.mark.asyncio
async def test_double_enter_on_empty_address_keeps_single_probe_line(tmp_path):
    """То же для пустого (не только невалидного) адреса — двойной Enter не плодит
    вторую строку-пробу и не роняет приложение."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr.query_one(
            ConnectionEditor
        ).escape_presets()  # список свёрнут — поле по-прежнему пустое
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
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._step == 1
        assert (
            scr._values["completions_url"] == "https://h/v1/chat/completions"
        )


@pytest.mark.asyncio
async def test_pagedown_keeps_validation_of_connection_step(tmp_path):
    """…с той же валидацией, что Enter-далее: невалидный адрес PgDn не пропускает."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
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
async def test_pagedown_advances_from_mcp_step(tmp_path, monkeypatch):
    """PgDn работает и на шагах без пометки (MCP) — зеркало PgUp.

    MCP → «Голосовой ввод»: тело шага голоса зовёт voice_setup.status_cheap, а
    _screen() не инжектит voice= — без изоляции тест стучался бы в РЕАЛЬНЫЙ каталог
    данных агента (voice_runtime.state.load_state); в сеть снимок не ходит
    (канон дешёвых снимков) — тот же приём, что у соседних тестов файла
    (test_final_summary_notice_*). Список вызовов подтверждает, что подмена
    реально сработала на этом прогоне, а не осталась мёртвым кодом."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_MCP, STEP_VOICE
    from gc_tui.voice_runtime import paths as vpaths

    calls = []

    def _fake_state_path():
        calls.append(1)
        return tmp_path / "voice_data" / "state.json"

    monkeypatch.setattr(
        vpaths, "voice_data_dir", lambda: tmp_path / "voice_data"
    )
    monkeypatch.setattr(
        "gc_tui.voice_runtime.state.voice_state_path", _fake_state_path
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_MCP)
        await pilot.pause()
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._step_id() == STEP_VOICE
        assert calls, (
            "изоляция не сработала — voice_runtime.state обошёл подмену"
        )


@pytest.mark.asyncio
async def test_pagedown_does_not_commit_on_confirm_step(tmp_path, monkeypatch):
    """Финал подтверждают Enter'ом: навигационная PgDn конфиг не пишет."""
    pytest.importorskip("textual")
    from gc_core import onboarding_config_writer as w
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM

    written = []
    monkeypatch.setattr(
        w, "write_values", lambda path, vals, **kw: written.append(vals) or []
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._step_id() == STEP_CONFIRM and written == []


@pytest.mark.asyncio
async def test_pgup_from_confirm_returns_to_mcp_step(tmp_path):
    """С финала PgUp — обычный шаг назад, на предыдущий по составу шаг."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM, STEP_VOICE

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        await pilot.press("pageup")
        await pilot.pause()
        assert scr._step_id() == STEP_VOICE


@pytest.mark.asyncio
async def test_model_enter_marks_then_advances(tmp_path):
    """Шаг «Модель»: Enter#1 помечает подсвеченную (● переезжает на неё), Enter#2 — далее."""
    pytest.importorskip("textual")
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            fetch=lambda *a, **k: [
                ModelInfo("Qwen", tool_capable=True),
                ModelInfo("Flash", tool_capable=True),
            ]
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._step = 1
        scr._render_step()
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step == 1
        assert scr._values["model"] == "Qwen"
        assert any(
            "● Qwen" in str(w.render()) for w in scr.query(".onb-option")
        )
        await pilot.press("enter")
        await pilot.pause()
        from gc_tui.screens.onboarding import STEP_LEVELS

        assert scr._step_id() == STEP_LEVELS


@pytest.mark.asyncio
async def test_model_footer_and_pagedown(tmp_path):
    """Футер шага модели меняется по пометке; PgDn ведёт дальше и без неё."""
    pytest.importorskip("textual")
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from textual.widgets import Static

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            fetch=lambda *a, **k: [ModelInfo("Qwen", tool_capable=True)]
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._step = 1
        scr._render_step()
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        footer = str(scr.query_one("#onb-footer", Static).render()).replace(
            FOOTER_SCROLL, ""
        )
        assert "Enter — выбрать" in footer and "PgDn — далее" in footer
        await pilot.press("pagedown")
        await pilot.pause()
        from gc_tui.screens.onboarding import STEP_LEVELS

        assert scr._step_id() == STEP_LEVELS
        assert scr._values["model"] == "Qwen"


@pytest.mark.asyncio
async def test_model_manual_row_two_beat(tmp_path):
    """Своё имя — один такт: строки-посредника больше нет (смок 2026-08-04), поэтому
    метить нечего и Enter сразу ведёт дальше, унося набранное."""
    pytest.importorskip("textual")
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from textual.widgets import Input

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            fetch=lambda *a, **k: [ModelInfo("Qwen", tool_capable=True)]
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._step = 1
        scr._render_step()
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        # Поле само себе фильтр: вписываем имя, которого нет в списке.
        scr.query_one("#onb-model", Input).value = "my-model"
        await pilot.pause()
        await pilot.press("enter")  # метить нечего → сразу «далее»
        await pilot.pause()
        from gc_tui.screens.onboarding import STEP_LEVELS

        assert (
            scr._step_id() == STEP_LEVELS
            and scr._values["model"] == "my-model"
        )


@pytest.mark.asyncio
async def test_model_footer_follows_filter_reselect(tmp_path):
    """Фильтрация двигает курсор (строка исчезла → якорь): роль Enter меняется — футер
    обязан догнать, как на ↑↓."""
    pytest.importorskip("textual")
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from textual.widgets import Input, Static

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            config={
                "backend.completions_url": "https://h/v1/chat/completions",
                "backend.chat_completion_headers": {},
                "agent.model": "Qwen",
            },
            fetch=lambda *a, **k: [
                ModelInfo("Qwen", tool_capable=True),
                ModelInfo("Flash", tool_capable=True),
            ],
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._step = 1
        scr._render_step()
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        await pilot.pause()
        await pilot.press("down")  # → Flash (не помечена)
        await pilot.pause()
        assert "Enter — выбрать" in str(
            scr.query_one("#onb-footer", Static).render()
        ).replace(FOOTER_SCROLL, "")
        scr.query_one(
            "#onb-model", Input
        ).value = "qw"  # Flash уходит → курсор на Qwen
        await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render()).replace(
            FOOTER_SCROLL, ""
        )
        assert "Enter — далее" in footer and "Enter — выбрать" not in footer


@pytest.mark.asyncio
async def test_model_footer_says_next_when_no_list(tmp_path):
    """Сервер не отдал список — остаётся ручной ввод: помечать нечего, Enter ведёт
    дальше, и футер обещает «далее», а не «выбрать»."""
    pytest.importorskip("textual")
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from textual.widgets import Input, Static

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(fetch=lambda *a, **k: None)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._values["completions_url"] = "https://h/v1/chat/completions"
        scr._step = 1
        scr._render_step()
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render()).replace(
            FOOTER_SCROLL, ""
        )
        assert "Enter — далее" in footer and "Enter — выбрать" not in footer
        scr.query_one("#onb-model", Input).value = "my-model"
        await pilot.press("enter")
        await pilot.pause()
        from gc_tui.screens.onboarding import STEP_LEVELS

        assert (
            scr._step_id() == STEP_LEVELS
            and scr._values["model"] == "my-model"
        )


@pytest.mark.asyncio
async def test_model_footer_marked_after_list_loads(tmp_path):
    """Модель уже задана в конфиге: список догрузился, курсор встал на помеченную —
    футер честно обещает «Enter — далее» (список приезжает воркером, футер догоняет)."""
    pytest.importorskip("textual")
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from textual.widgets import Static

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            config={
                "backend.completions_url": "https://h/v1/chat/completions",
                "backend.chat_completion_headers": {},
                "agent.model": "Qwen",
            },
            fetch=lambda *a, **k: [
                ModelInfo("Qwen", tool_capable=True),
                ModelInfo("Flash", tool_capable=True),
            ],
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._step = 1
        scr._render_step()
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render()).replace(
            FOOTER_SCROLL, ""
        )
        assert "Enter — далее" in footer and "Enter — выбрать" not in footer


@pytest.mark.asyncio
async def test_mcp_space_toggles(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_MCP, STEP_VOICE
    from test_onboarding_voice_step import FakeVoice

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(voice=FakeVoice())
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_MCP)
        await pilot.pause()
        await pilot.press("space")  # отметить первый (duckduckgo)
        await pilot.pause()
        assert "duckduckgo" in scr._mcp_selected
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step_id() == STEP_VOICE


@pytest.mark.asyncio
async def test_summary_line_fills_as_steps_complete(tmp_path):
    """Строка сводки под степпером заполняется по ходу; токен маскируется."""
    from gc_tui import app as app_mod
    from gc_tui.screens.onboarding import OnboardingScreen
    from textual.widgets import Static

    app = app_mod.TuiApp(
        settings=tui_settings(
            {
                "backend.completions_url": "",
                "backend.chat_completion_headers": {},
                "agent.model": "",
            }
        )
    )
    async with app.run_test() as pilot:
        scr = OnboardingScreen(
            settings=app.base_settings(),
            config_path=str(tmp_path / "c.yml"),
            review_all=True,
            on_commit=lambda: None,
        )
        app.push_screen(scr)
        await pilot.pause()
        scr._values["completions_url"] = (
            "https://46.243.227.147:15000/v1/chat/completions"
        )
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
    from gc_core import onboarding
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM

    cfg_path = _write_conn_cfg(
        tmp_path
    )  # чистый config.yml из пакетного шаблона
    committed = []
    installed = []
    mcp = _FakeMcp()
    mcp.install = lambda name=None: installed.append(name) or 0
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            on_commit=lambda: committed.append(True),
            mcp=mcp,
            config_path=str(cfg_path),
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._values = {
            "completions_url": "https://h/v1",
            "Authorization": "Bearer sk",
            "model": "m",
        }
        scr._mcp_selected = {"office"}
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        await pilot.press("enter")
        await wait_until(pilot, lambda: committed, "commit done")
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert raw["backend"]["completions_url"] == "https://h/v1"
        assert (
            raw["backend"]["chat_completion_headers"]["Authorization"]
            == "Bearer sk"
        )
        assert raw["models"] == {"m": {"model": "m"}}
        assert raw["slots"]["default"] == "m"
        assert (
            raw["agent"]["model"] == onboarding.MODEL_SENTINEL
        )  # легаси-ключ не тронут
        assert installed == ["office"]
        assert committed == [True]


def _registry_cfg(tmp_path, model_id, *, alias):
    """Конфиг, настроенный ЭТОЙ ЖЕ веткой: подключение — плоскими ключами, модель —
    записью реестра (`models.<алиас>` + `slots.default`), а легаси `agent.model` так
    и остался шаблонным сентинелом. Ровно то, что оставляет после себя
    `write_wizard_values`, — и ровно то, поверх чего мастер открывают повторно.

    Возвращает (путь к файлу, конфиг «как его видит приложение»)."""
    from pathlib import Path

    import yaml
    from gc_core.config import _default_config_path
    from gc_core.config_modules.registry_write import apply_registry_ops

    cfg_path = _write_conn_cfg(
        tmp_path, url="https://h/v1/chat/completions", auth="Bearer tok"
    )
    template = Path(_default_config_path()).read_text(encoding="utf-8")
    apply_registry_ops(
        cfg_path,
        [
            ("upsert_model", alias, {"model": model_id}),
            ("write_slot", "default", alias),
        ],
        template,
    )
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = _resolved_conn_cfg(cfg_path)
    cfg["models"] = raw.get("models")
    cfg["slots"] = raw.get("slots")
    return cfg_path, cfg


@pytest.mark.asyncio
async def test_review_on_registry_config_keeps_the_configured_model(tmp_path):
    """Мастер поверх РЕЕСТРОВОГО конфига (`gc-ua --init-config` на уже настроенном):
    действующая модель обязана быть ему известна — сводка её называет, шаг «Модель»
    ставит курсор на строку «текущая», а проход насквозь (PgDn) слот не переставляет.

    Пока шаг якорился на плоском `agent.model`, действующей модели для мастера не
    существовало вовсе: сводка говорила «модель: —», курсор вставал на ПЕРВУЮ модель
    из ответа сервера, и один PgDn молча уводил `slots.default` на чужую. Соседние
    review-тесты живут на легаси-конфиге (`agent.model` задан) и этого не видели."""
    pytest.importorskip("textual")
    import yaml
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from gc_tui.screens.onboarding import STEP_MODEL
    from textual.widgets import Static

    cfg_path, cfg = _registry_cfg(
        tmp_path, "openai/gpt-5.6-terra", alias="gpt-5.6-terra"
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            review_all=True,
            config_path=str(cfg_path),
            config=cfg,
            fetch=lambda *a, **k: [
                ModelInfo("glm-5.2-fp8", tool_capable=True),
                ModelInfo("openai/gpt-5.6-terra", tool_capable=True),
            ],
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        summary = str(scr.query_one("#onb-summary", Static).render())
        assert "модель: openai/gpt-5.6-terra" in summary
        goto_step(scr, STEP_MODEL)
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        await pilot.pause()
        rows = [str(w.render()) for w in scr.query(".onb-option") if w.display]
        current = [r for r in rows if "текущая" in r]
        assert current, f"строки «текущая» на шаге нет: {rows}"
        assert "openai/gpt-5.6-terra" in current[0]
        # Курсор ▸ и пометка ● — на ней же: Enter/PgDn отсюда подтверждают своё, не чужое.
        assert current[0].startswith("▸") and "●" in current[0]
        await pilot.press("pagedown")  # проход насквозь, ничего не выбирая
        await pilot.pause()
        assert scr._values.get("model") == "openai/gpt-5.6-terra"
        assert scr._commit_config() is True
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert raw["slots"]["default"] == "gpt-5.6-terra"
        assert raw["models"] == {
            "gpt-5.6-terra": {"model": "openai/gpt-5.6-terra"}
        }


@pytest.mark.asyncio
async def test_model_step_passthrough_keeps_the_fallback_when_default_is_unset(
    tmp_path,
):
    """Прогон ревьюера (M12): `slots: {light: slim}`, default не задан. Строки
    «текущая» не было, предвыбор вставал на ПЕРВУЮ модель сервера, и один PgDn
    молча писал `slots.default` на неё — резолв default уезжал с vendor/slim
    (фоллбэк) на srv/first. Строка «сейчас» обязана показать модель фоллбэка с
    подписью об источнике (правило — в ядре, общее с плоским мастером), предвыбор
    — встать на неё, а проход насквозь — оставить резолв прежним."""
    pytest.importorskip("textual")
    from pathlib import Path

    import yaml
    from gc_core.config import _default_config_path
    from gc_core.config_modules.registry_write import apply_registry_ops
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from gc_tui.screens.onboarding import STEP_MODEL

    cfg_path = _write_conn_cfg(
        tmp_path, url="https://h/v1/chat/completions", auth="Bearer tok"
    )
    template = Path(_default_config_path()).read_text(encoding="utf-8")
    apply_registry_ops(
        cfg_path,
        [
            ("upsert_model", "slim", {"model": "vendor/slim"}),
            ("write_slot", "light", "slim"),
        ],
        template,
    )
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = _resolved_conn_cfg(cfg_path)
    cfg["models"], cfg["slots"] = raw.get("models"), raw.get("slots")
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            review_all=True,
            config_path=str(cfg_path),
            config=cfg,
            fetch=lambda *a, **k: [
                ModelInfo("srv/first", tool_capable=True),
                ModelInfo("vendor/slim", tool_capable=True),
            ],
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_MODEL)
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        await pilot.pause()
        rows = [str(w.render()) for w in scr.query(".onb-option") if w.display]
        current = [r for r in rows if "текущая" in r]
        assert current, f"строки «текущая» на шаге нет: {rows}"
        assert "vendor/slim" in current[0]
        assert current[0].startswith("▸")  # предвыбор — на модели фоллбэка
        hints = " ".join(str(w.render()) for w in scr.query(".onb-hint"))
        assert "default не задан — работает light" in hints
        await pilot.press("pagedown")  # проход насквозь, ничего не выбирая
        await pilot.pause()
        assert scr._values.get("model") == "vendor/slim"
        assert scr._commit_config() is True
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert raw["models"] == {
            "slim": {"model": "vendor/slim"}
        }  # близнецов не завелось
        assert raw["slots"] == {
            "light": "slim",
            "default": "slim",
        }  # резолв — прежний vendor/slim


def _two_level_cfg(tmp_path, *, level):
    """Конфиг с ДВУМЯ назначенными уровнями и активным `level`: единственная
    раскладка, на которой видно, про какой уровень говорит шаг «Модель»."""
    import yaml
    from gc_core.config_modules.registry_write import apply_registry_ops

    cfg_path, cfg = _registry_cfg(tmp_path, "qwen/qwen3-8b", alias="fast")
    apply_registry_ops(
        cfg_path,
        [
            ("upsert_model", "brain", {"model": "qwen/qwen3-235b"}),
            ("write_slot", "max", "brain"),
        ],
        "",
    )
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg["models"], cfg["slots"] = raw.get("models"), raw.get("slots")
    cfg["model"] = level
    return cfg_path, cfg


@pytest.mark.asyncio
async def test_model_step_marks_the_default_slot_not_the_active_level(
    tmp_path,
):
    """Тот же промах, что чинили в плоском мастере (M6), — здесь: «текущей» бралась
    модель `onboarding.effective_model` — резолв АКТИВНОГО уровня. При `level: max` шаг про
    слот `default` метил строку модели max, и Enter/PgDn отсюда переставлял `default`
    на чужую модель.

    Правило одно на обе поверхности и живёт в ядре (`onboarding.model_step_current`):
    вторая его редакция — ровно то, из-за чего дефект и уцелел в TUI."""
    pytest.importorskip("textual")
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from gc_tui.screens.onboarding import STEP_MODEL

    cfg_path, cfg = _two_level_cfg(tmp_path, level="max")
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            review_all=True,
            config_path=str(cfg_path),
            config=cfg,
            fetch=lambda *a, **k: [
                ModelInfo("qwen/qwen3-8b", tool_capable=True),
                ModelInfo("qwen/qwen3-235b", tool_capable=True),
            ],
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_MODEL)
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        await pilot.pause()
        rows = [str(w.render()) for w in scr.query(".onb-option") if w.display]
        current = [r for r in rows if "текущая" in r]
        assert current, f"строки «текущая» на шаге нет: {rows}"
        assert "qwen/qwen3-8b" in current[0] and "235b" not in current[0]


@pytest.mark.asyncio
async def test_confirm_row_of_the_default_slot_ignores_the_active_level(
    tmp_path,
):
    """Строка витрины названа `модель default:` — и обязана говорить про слот
    `default`. Она читала `_shown_values`, то есть тот же резолв активного уровня:
    на одном экране рядом стояли «модель default: <модель max>» и «модель max: <она
    же>», а Enter записывал третье."""
    pytest.importorskip("textual")
    import re

    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM

    cfg_path, cfg = _two_level_cfg(tmp_path, level="max")
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(review_all=True, config_path=str(cfg_path), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query("Static.onb-dyn"))
        flat = re.sub(r"\s+", " ", body)
        assert "модель default: qwen/qwen3-8b" in flat
        assert "модель max: qwen/qwen3-235b" in flat


@pytest.mark.asyncio
async def test_confirm_summary_shows_the_fallback_model_when_default_is_unset(
    tmp_path,
):
    """Ревью #14: M12 (`onboarding.model_step_display`) переехал на шаг «Модель» —
    строка «сейчас» уже пишет «сейчас: qwen/qwen3-235b (default не задан —
    работает max)», — но не на сводку/ленту подтверждения: `_shown_values`
    по-прежнему звала старый `onboarding.model_step_current`, который при
    незаданном `default` и заданном другом уровне честно отвечает пусто (правило
    «не выдумывать назначенное»), и витрина показывала «модель default: —» на
    том же экране, где шаг уже назвал модель фоллбэка. Плоский мастер
    (`_summary_lines`) на `default_fallback_model` переехал раньше — TUI-близнец
    остался на старом резолве."""
    pytest.importorskip("textual")
    import re
    from pathlib import Path

    import yaml
    from gc_core.config import _default_config_path
    from gc_core.config_modules.registry_write import apply_registry_ops
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM

    cfg_path = _write_conn_cfg(
        tmp_path, url="https://h/v1/chat/completions", auth="Bearer tok"
    )
    template = Path(_default_config_path()).read_text(encoding="utf-8")
    apply_registry_ops(
        cfg_path,
        [
            ("upsert_model", "brain", {"model": "qwen/qwen3-235b"}),
            ("write_slot", "max", "brain"),
        ],
        template,
    )
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = _resolved_conn_cfg(cfg_path)
    cfg["models"], cfg["slots"] = raw.get("models"), raw.get("slots")
    cfg["model"] = "max"
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(review_all=True, config_path=str(cfg_path), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query("Static.onb-dyn"))
        flat = re.sub(r"\s+", " ", body)
    assert "модель default: qwen/qwen3-235b" in flat, flat
    assert "модель default: —" not in flat, flat


@pytest.mark.asyncio
async def test_both_wizards_take_the_current_model_of_the_step_from_one_place(
    tmp_path, monkeypatch
):
    """Инвариант межповерхностный, поэтому и тест один на оба мастера: у каждого в
    отдельности своя редакция правила выглядела бы исправной — так дефект и жил,
    починенный в плоском и уцелевший в TUI. Подменяем ЕДИНСТВЕННЫЙ дом правила и
    ждём подмену в обоих."""
    pytest.importorskip("textual")
    import re

    from gc_core import onboarding

    # gc-tui от gc-cli НЕ зависит: тест межповерхностный, и в наборе одного
    # пакета он законен только как условный. importorskip, а не import:
    # автономный прогон `cd packages/tui && pytest` обязан проходить БЕЗ
    # соседнего дистрибутива, иначе зелёный цвет означал бы лишь то, что
    # сосед оказался в общем окружении (см. tests/test_test_ownership.py).
    wz = pytest.importorskip("gc_cli.onboarding_wizard")
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM

    sentinel = "модель-из-общего-дома"
    monkeypatch.setattr(
        onboarding, "model_step_current", lambda config: sentinel
    )

    printed: list = []
    io = wz.WizardIO(reader=lambda p, secret: "", printer=printed.append)
    wz._collect_model(io, make_settings(), {}, fetch=lambda *a, **k: None)
    assert any(sentinel in line for line in printed)

    cfg_path, cfg = _two_level_cfg(tmp_path, level="max")
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(review_all=True, config_path=str(cfg_path), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query("Static.onb-dyn"))
        assert f"модель default: {sentinel}" in re.sub(r"\s+", " ", body)


# --- ссылки на переменные окружения ${ENV:VAR} (настоящий файл + настоящий os.environ) ---
# Критический регресс: шаг «Подключение» показывал/писал РЕЗОЛВНУТОЕ значение
# self._config, а не сырое из файла — непройденный проход мастера подменял ссылку
# ${ENV:...} действующим секретом открытым текстом (или пустой строкой, если
# переменной в этой сессии нет). Идём реальным путём записи (_commit_config →
# файл на диске), а не проверкой _values — тот путь бага не увидел бы.


@pytest.mark.asyncio
async def test_env_refs_survive_untouched_connection_step(
    tmp_path, monkeypatch
):
    """Адрес, токен и прокси — все три ссылки ${ENV:...}, ни одно поле не тронуто:
    после полного прохода шага и коммита файл не изменился ни в одной из трёх строк,
    и ни один резолвнутый секрет в него не попал."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    monkeypatch.setenv(
        "GC_URL", "https://real-server.example/v1/chat/completions"
    )
    monkeypatch.setenv("GC_TOKEN", "sk-REALSECRET123")
    monkeypatch.setenv("GC_PROXY", "http://real-corp-proxy:8080")
    cfg_path = _write_conn_cfg(
        tmp_path,
        url="${ENV:GC_URL}",
        auth="Bearer ${ENV:GC_TOKEN}",
        proxy="${ENV:GC_PROXY}",
        model="m",
    )
    original = cfg_path.read_text(encoding="utf-8")
    cfg = _resolved_conn_cfg(cfg_path)
    assert (
        cfg["backend.completions_url"]
        == "https://real-server.example/v1/chat/completions"
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(review_all=True, config_path=str(cfg_path), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        from gc_tui.editors.connection import ConnectionEditor

        scr.query_one(ConnectionEditor).escape_presets()
        scr._submit_connection()  # ни одно поле не тронуто
        assert "completions_url" not in scr._values
        assert "Authorization" not in scr._values
        assert "proxy" not in scr._values
        assert scr._commit_config() is True
        content = cfg_path.read_text(encoding="utf-8")
        assert content == original  # файл не тронут вовсе
        assert "sk-REALSECRET123" not in content  # резолвнутый секрет не утёк
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
    from gc_tui.app import TuiApp

    monkeypatch.delenv("GC_TOKEN", raising=False)
    cfg_path = _write_conn_cfg(
        tmp_path,
        url="https://h/v1/chat/completions",
        auth="Bearer ${ENV:GC_TOKEN}",
        model="m",
    )
    cfg = _resolved_conn_cfg(cfg_path)
    assert (
        cfg["backend.chat_completion_headers"]["Authorization"] == "Bearer "
    )  # резолв в пустоту
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(review_all=True, config_path=str(cfg_path), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        from gc_tui.editors.connection import ConnectionEditor

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
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from textual.widgets import Input

    monkeypatch.setenv("GC_TOKEN", "sk-OLDSECRET")
    cfg_path = _write_conn_cfg(
        tmp_path,
        url="https://h/v1/chat/completions",
        auth="Bearer ${ENV:GC_TOKEN}",
        model="m",
    )
    cfg = _resolved_conn_cfg(cfg_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(review_all=True, config_path=str(cfg_path), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one("#conn-token", Input).value = "sk-typed-by-hand"
        scr._submit_connection()
        assert scr._values["Authorization"] == "sk-typed-by-hand"
        assert scr._commit_config() is True
        content = cfg_path.read_text(encoding="utf-8")
        assert 'Authorization: "sk-typed-by-hand"' in content
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
    from gc_tui.app import TuiApp

    monkeypatch.setenv("GC_TOKEN", "sk-REALSECRET123")
    cfg_path = _write_conn_cfg(
        tmp_path,
        url="https://h/v1/chat/completions",
        auth="Bearer ${ENV:GC_TOKEN}",
    )  # model не передан — остаётся реальным
    # значением шаблона, не сентинелом
    cfg = _resolved_conn_cfg(cfg_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(review_all=False, config_path=str(cfg_path), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        from gc_tui.editors.connection import ConnectionEditor

        scr.query_one(ConnectionEditor).escape_presets()
        scr._submit_connection()  # ни одно поле не тронуто — Enter'ом как есть
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
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.services.settings_probe import ProbeResult

    monkeypatch.setenv(
        "GC_URL", "https://real-server.example/v1/chat/completions"
    )
    cfg_path = _write_conn_cfg(tmp_path, url="${ENV:GC_URL}")
    cfg = _resolved_conn_cfg(cfg_path)
    seen = {}

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        seen["endpoint"] = endpoint
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path), config=cfg, probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._auto_probe()
        await wait_until(pilot, lambda: "endpoint" in seen, "проба ушла")
        assert (
            seen["endpoint"]
            == "https://real-server.example/v1/chat/completions"
        )


@pytest.mark.asyncio
async def test_auto_probe_resolution_does_not_leak_into_field_or_file(
    tmp_path, monkeypatch
):
    """Парный к тесту выше: раскрытие адреса для пробы — только для самого
    запроса, оно не оседает ни в поле, ни в _values, ни в файле."""
    pytest.importorskip("textual")
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from gc_tui.services.settings_probe import ProbeResult
    from textual.widgets import Input

    monkeypatch.setenv(
        "GC_URL", "https://real-server.example/v1/chat/completions"
    )
    cfg_path = _write_conn_cfg(tmp_path, url="${ENV:GC_URL}")
    cfg = _resolved_conn_cfg(cfg_path)
    seen = {}

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        seen["endpoint"] = endpoint
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path), config=cfg, probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._auto_probe()
        await wait_until(pilot, lambda: "endpoint" in seen, "проба ушла")
        ed = scr.query_one(ConnectionEditor)
        assert ed.query_one("#conn-url", Input).value == "${ENV:GC_URL}"
        ed.escape_presets()
        scr._submit_connection()  # поле не тронуто
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
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.services.settings_probe import ProbeResult

    cfg_path = _write_conn_cfg(
        tmp_path,
        url="https://h/v1/chat/completions",
        auth="Bearer sk-REALSECRET123",
    )
    cfg = _resolved_conn_cfg(cfg_path)
    seen = []

    # Сервер, ТРЕБУЮЩИЙ авторизацию: иначе токену просто некуда доехать —
    # проба, получив ok на контрольный запрос, второй раз не пойдёт.
    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        seen.append(auth)
        if auth is None:
            return ProbeResult(
                "rejected", 401, None, 5, "токен отклонён (401)"
            )
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path), config=cfg, probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._auto_probe()
        await wait_until(pilot, lambda: len(seen) >= 2, "проба ушла с токеном")
        assert seen == [None, "Bearer sk-REALSECRET123"]


@pytest.mark.asyncio
async def test_auto_probe_resolves_env_ref_token_when_field_untouched(
    tmp_path, monkeypatch
):
    """То же, но токен в файле записан ссылкой ${ENV:...}: до пробы он доезжает уже
    раскрытым, потому что фолбэк берёт его из self._config (тот резолвится при
    загрузке). Тест сторожит именно это — что ссылка в файле не мешает пробе уйти
    с настоящим секретом. Раскрытие ссылки, НАБРАННОЙ на самом шаге, не покрыто:
    поле токена здесь принципиально пустое, и такой путь сейчас недостижим."""
    pytest.importorskip("textual")
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.services.settings_probe import ProbeResult

    monkeypatch.setenv("GC_TOKEN", "sk-REALSECRET123")
    cfg_path = _write_conn_cfg(
        tmp_path,
        url="https://h/v1/chat/completions",
        auth="Bearer ${ENV:GC_TOKEN}",
    )
    cfg = _resolved_conn_cfg(cfg_path)
    seen = []

    # Сервер, ТРЕБУЮЩИЙ авторизацию (см. соседний тест): без 401 на контрольный
    # запрос проба до токена не дойдёт и проверять было бы нечего.
    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        seen.append(auth)
        if auth is None:
            return ProbeResult(
                "rejected", 401, None, 5, "токен отклонён (401)"
            )
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path), config=cfg, probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._auto_probe()
        await wait_until(pilot, lambda: len(seen) >= 2, "проба ушла с токеном")
        assert seen == [None, "Bearer sk-REALSECRET123"]


# --- гейт «пустой адрес» на ссылке ${ENV:...}, у которой нет переменной ---
# Гейт судил СЫРОЕ значение поля: непустая ссылка ${ENV:GC_URL} его проходит, но
# резолв даёт "" (переменной нет), и проба реально стучалась в пустой адрес — под
# визуально заполненным полем показывалось обманчивое «нет соединения (пустой
# адрес)», как будто мастер сломан.


@pytest.mark.asyncio
async def test_auto_probe_reports_missing_env_var_instead_of_probing(
    tmp_path, monkeypatch
):
    """Переменной в этой сессии нет: проба не должна стартовать вовсе, статус —
    честный про переменную, а не про «пустой адрес»/«нет соединения»."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    monkeypatch.delenv("GC_URL", raising=False)
    cfg_path = _write_conn_cfg(tmp_path, url="${ENV:GC_URL}")
    cfg = _resolved_conn_cfg(cfg_path)
    called = {"n": 0}

    def fake_probe(*a, **k):
        called["n"] += 1
        from gc_tui.services.settings_probe import ProbeResult

        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path), config=cfg, probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._auto_probe()
        await pilot.pause()
        assert called["n"] == 0  # проба не ушла
        line = str(scr.query_one(".onb-conn-probe").render())
        assert "GC_URL" in line
        assert "переменной нет" in line
        assert "пустой адрес" not in line
        assert "нет соединения" not in line


@pytest.mark.asyncio
async def test_ctrl_u_clears_the_saved_token_and_probes_without_it(tmp_path):
    """Ctrl+U на поле токена шага «Подключение» стирает СОХРАНЁННЫЙ токен — ту же
    клавишу мастер держит, что и /settings, и гейтит её тем же `clear_token()` ровно
    на этом поле (на адресе она молчит). Поле токена пустое (секрет живёт маской в
    placeholder), так что штатный `delete_left_all` у Input здесь вхолостую, и
    обработчик экрана обязан перехватить клавишу сам.

    Второе — проба: раз токен помечен к удалению, она обязана уйти БЕЗ него ещё до
    сохранения (`_auto_probe`, ветка `token_cleared`). Иначе мастер показывал бы
    вердикт про конфигурацию, которой после Enter уже не будет.

    Сервер здесь ТРЕБУЕТ авторизацию (401 на контрольный запрос): без этого токену
    некуда доехать, и «ушла без токена» вышло бы верным само собой."""
    pytest.importorskip("textual")
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import _TOKEN_PH_CLEARED, ConnectionEditor
    from gc_tui.services.settings_probe import ProbeResult
    from textual.widgets import Input

    cfg_path = _write_conn_cfg(
        tmp_path,
        url="https://h/v1/chat/completions",
        auth="Bearer sk-REALSECRET123",
    )
    cfg = _resolved_conn_cfg(cfg_path)
    seen = []

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        seen.append(auth)
        if auth is None:
            return ProbeResult(
                "rejected", 401, None, 5, "токен отклонён (401)"
            )
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path), config=cfg, probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        token = ed.query_one("#conn-token", Input)
        assert "REALSECRET" not in token.placeholder  # маска, а не секрет
        assert (
            token.placeholder != _TOKEN_PH_CLEARED
        )  # …но маска сохранённого есть

        ed.move(1)  # → поле токена: там клавиша и обещана
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert ed.token_cleared is True
        assert (
            token.placeholder == _TOKEN_PH_CLEARED
        )  # маска ушла, обещано удаление

        scr._auto_probe()
        await wait_until(pilot, lambda: seen, "проба ушла")
        await pilot.pause()
        assert seen == [None], "проба потащила стёртый токен"


async def test_ctrl_space_reprobes_the_connection_step(tmp_path):
    """Ctrl+Space на поле «Подключения» шлёт пробу — и шлёт её ПОВТОРНО, хотя поля не
    менялись: снимок против дребезга гасит только автопробу, ручную глушить нельзя
    (спека §5.2 канона; развод клавиш — спека 2026-08-06 §2)."""
    pytest.importorskip("textual")
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from gc_tui.services.settings_probe import ProbeResult
    from textual.widgets import Input

    calls = []

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        calls.append(endpoint)
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        await pilot.pause()
        assert not calls  # печать сама по себе пробу не шлёт
        # Развод клавиш (спека 2026-08-06 §2): Ctrl+Space больше не спрашивает список —
        # одно нажатие сразу уходит в пробу.
        await pilot.press("ctrl+space")
        await wait_until(
            pilot, lambda: len(calls) == 1, "ручная проверка ушла"
        )
        await pilot.press("ctrl+space")  # поля не трогали
        await wait_until(
            pilot, lambda: len(calls) == 2, "снимок не глушит ручную"
        )


@pytest.mark.asyncio
async def test_alt_down_on_the_model_step_reloads_the_list(tmp_path):
    """Шаг «Модель» мастера — то же поле со списком с сервера, что и секция «Модель»
    в /settings (§9.1 канона): Alt+↓ идёт за списком заново (спека 2026-08-06 §2 —
    Ctrl+Space на этом шаге пробы не даёт, см. `test_ctrl_space_on_the_model_step_does_nothing`).
    Сервер лежал (одинокое поле), ожил — список собирается."""
    pytest.importorskip("textual")
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor

    calls = []

    def fetch(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return None  # сервер лежал
        return [ModelInfo("glm-5.2-fp8", tool_capable=True)]

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            config={
                "backend.completions_url": "https://h/v1/chat/completions",
                "backend.chat_completion_headers": {},
            },
            fetch=fetch,
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._step = 1
        scr._render_step()
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        assert not scr.query(".onb-option"), (
            "предусловие: сервер списка не отдал"
        )
        await pilot.press("alt+down")
        await wait_until(
            pilot,
            lambda: any(
                "glm-5.2-fp8" in str(w.render())
                for w in scr.query(".onb-option")
            ),
            "список перечитан с сервера",
        )


@pytest.mark.asyncio
async def test_ctrl_space_on_the_model_step_does_nothing(tmp_path):
    """Пробы на шаге «Модель» нет (§2.5, §5.1 спеки 2026-08-06) — Ctrl+Space
    съедается молча: список не трогает, шаг не листает, не падает."""
    pytest.importorskip("textual")
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor

    def fetch(*a, **k):
        return [ModelInfo("glm-5.2-fp8", tool_capable=True)]

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            config={
                "backend.completions_url": "https://h/v1/chat/completions",
                "backend.chat_completion_headers": {},
            },
            fetch=fetch,
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._step = 1
        scr._render_step()
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        rows_before = [str(w.render()) for w in scr.query(".onb-option")]
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert app.screen is scr
        assert scr._step == 1
        assert [
            str(w.render()) for w in scr.query(".onb-option")
        ] == rows_before


@pytest.mark.asyncio
async def test_ctrl_space_on_an_empty_address_says_what_is_missing(tmp_path):
    """Молчать в ответ на клавишу нельзя (канон draft-probe /settings, где тот же
    случай прямо оговорён комментарием). На чистой установке шаг «Подключение»
    стартует с ПУСТЫМ адресом — именно там новичок жмёт клавишу первым делом, а
    _auto_probe выходит на `if not url` до записи любой строки."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from gc_tui.services.settings_probe import ProbeResult
    from textual.widgets import Input

    calls = []

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        calls.append(endpoint)
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        await pilot.pause()
        assert not ed.query_one(
            "#conn-url", Input
        ).value  # предусловие: адрес пуст
        # Развод клавиш (спека 2026-08-06 §2): Ctrl+Space сразу про пробу.
        await pilot.press("ctrl+space")
        await pilot.pause()
        line = " ".join(str(w.render()) for w in scr.query(".onb-conn-probe"))
        assert "сначала настройте адрес" in line
        assert not calls, "стучаться в пустой адрес нечем"


@pytest.mark.asyncio
async def test_ctrl_space_on_an_env_reference_still_probes(
    tmp_path, monkeypatch
):
    """Регресс ревью (фикс-раунд 1): шаг «Подключение» держит СЫРОЕ значение файла
    (`_write_conn_cfg`, без резолва) — `${ENV:GC_URL}` там штатное состояние, не
    фильтр. §2.3 бракует её так же, как «gr» (`_valid_endpoint_url` требует http(s)),
    но блокировать пробу здесь нельзя: корп-конфиг на переменных окружения — рабочий
    сценарий, а не край. Ctrl+Space обязан довести до `_auto_probe`, реальным
    нажатием (не прямым вызовом)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from gc_tui.services.settings_probe import ProbeResult
    from textual.widgets import Input

    monkeypatch.setenv(
        "GC_URL", "https://real-server.example/v1/chat/completions"
    )
    cfg_path = _write_conn_cfg(tmp_path, url="${ENV:GC_URL}", model="m")
    cfg = _resolved_conn_cfg(cfg_path)
    calls = []

    def fake_probe(endpoint, auth, verify, model, *, config_proxy=None):
        calls.append(endpoint)
        return ProbeResult("ok", 200, None, 5, "доступен")

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(config_path=str(cfg_path), config=cfg, probe=fake_probe)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        await pilot.pause()
        assert (
            ed.query_one("#conn-url", Input).value == "${ENV:GC_URL}"
        )  # предусловие
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert calls, (
            "ссылка обязана дойти до пробы, а не быть принятой за фильтр"
        )
        line = " ".join(str(w.render()) for w in scr.query(".onb-conn-probe"))
        assert "сначала выберите адрес" not in line


@pytest.mark.asyncio
async def test_alt_down_on_the_model_step_goes_past_the_session_cache(
    tmp_path,
):
    """Мастер оборачивает свой fetch в тот же сессионный кэш (`OnboardingScreen.__init__`),
    значит и здесь перечитывание (Alt+↓) после УДАЧНОЙ загрузки обязано идти мимо
    него — иначе клавиша детерминированный no-op."""
    pytest.importorskip("textual")
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor

    calls = []

    def fetch(*a, **k):
        calls.append(1)
        base = [ModelInfo("glm-5.2-fp8", tool_capable=True)]
        if len(calls) == 1:
            return base  # что сервер отдавал сначала
        return base + [ModelInfo("deepseek-v3-base", tool_capable=True)]

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            config={
                "backend.completions_url": "https://h/v1/chat/completions",
                "backend.chat_completion_headers": {},
            },
            fetch=fetch,
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._step = 1
        scr._render_step()
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        rows = lambda: [str(w.render()) for w in scr.query(".onb-option")]
        assert any("glm-5.2-fp8" in r for r in rows())  # предусловие
        assert not any("deepseek" in r for r in rows())
        await pilot.press("alt+down")
        await wait_until(
            pilot,
            lambda: any("deepseek" in r for r in rows()),
            "перечитывание должно идти мимо сессионного кэша",
        )


# --- заглушка адреса из шаблона — не адрес: сетевой путь по ней не ходит -----


def test_endpoint_auth_does_not_hand_out_the_placeholder_address():
    """Нетронутый шаблонный конфиг: адреса ЕЩЁ НЕТ, и сетевой путь обязан это знать.
    Заглушка, отданная как адрес, уводила запрос за списком моделей на несуществующий
    `your-backend:PORT` — мастер печатал «не удалось получить список» при видимом в
    поле рабочем адресе человека."""
    scr = _screen()  # config = шаблон: оба сентинела на месте
    endpoint, auth = scr._endpoint_auth()
    assert endpoint == ""
    assert auth is None  # заглушку токена путь фильтровал и раньше


@pytest.mark.asyncio
async def test_model_step_on_placeholder_address_behaves_as_on_empty_one(
    tmp_path,
):
    """Шаг «Модель» на заглушке адреса = шаг «Модель» на чистой установке: в сеть не
    идём, списка нет, остаётся ручной ввод."""
    pytest.importorskip("textual")
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from gc_tui.screens.onboarding import STEP_MODEL

    dialed = []

    def fetch(endpoint, auth, **kw):
        dialed.append(endpoint)
        return [ModelInfo("glm-5.2-fp8", tool_capable=True)]

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(fetch=fetch)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_MODEL)
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "шаг отработал",
        )
        assert dialed == []
        assert scr.query(ModelEditor).first().has_models is False


@pytest.mark.asyncio
async def test_probe_verdict_is_pulled_into_frame_on_every_update(
    tmp_path, monkeypatch
):
    """Вердикт пробы обязан попадать в кадр — и на ПОВТОРНОЕ нажатие тоже.

    Строка стоит ниже поля «Прокси», тело шага длиннее вьюпорта рамки: без подводки
    человек жмёт Ctrl+Space и видит «вообще ничего не происходит» (живой смок).
    Подводка живёт у единственного писателя строки — заказанная при монтаже тела, она
    досталась бы только первому вердикту."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens import onboarding as onb
    from rich.text import Text

    revealed = []
    monkeypatch.setattr(
        onb, "reveal", lambda w: revealed.append(w), raising=False
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        line = scr.query_one(".onb-conn-probe")
        scr._set_conn_probe_line(Text("проверяю…"))
        scr._set_conn_probe_line(Text("✓ сервер отвечает"))
        assert revealed == [line, line]


# --- шаг уровней (в) — волна B, Task 8: две опциональные под-формы -----------
# Шаг живёт в СОСТАВЕ шагов, а не поверх него: сравнения кода идут по именам
# (`_step_id`), а не по номерам.


def _levels_screen(tmp_path, **over):
    """Мастер с пройденными шагами подключения и модели, стоящий на шаге уровней."""
    from gc_core.model_picker import ModelInfo

    cfg_path = _write_conn_cfg(tmp_path)
    kw = dict(
        config_path=str(cfg_path),
        fetch=lambda *a, **k: [
            ModelInfo("Flash", tool_capable=True),
            ModelInfo("Heavy", tool_capable=True),
        ],
    )
    kw.update(over)
    scr = _screen(**kw)
    scr._values.update(
        {
            "completions_url": "https://h/v1/chat/completions",
            "Authorization": "Bearer sk",
            "model": "Qwen",
        }
    )
    return cfg_path, scr


@pytest.mark.asyncio
async def test_levels_step_skipped_writes_nothing(tmp_path):
    """Шаг скипаемый: PgDn уводит дальше, ни одна модель уровня не собрана — в файл
    не уезжает ни записи, ни слота."""
    pytest.importorskip("textual")
    import yaml
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM, STEP_LEVELS, STEP_MCP

    committed = []
    cfg_path, scr = _levels_screen(
        tmp_path, on_commit=lambda: committed.append(True)
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_LEVELS)
        await pilot.pause()
        assert scr._step_id() == STEP_LEVELS
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._step_id() == STEP_MCP
        assert (
            "model_light" not in scr._values and "model_max" not in scr._values
        )
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
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
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from gc_tui.screens.onboarding import STEP_CONFIRM, STEP_LEVELS

    committed = []
    cfg_path, scr = _levels_screen(
        tmp_path, on_commit=lambda: committed.append(True)
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_LEVELS)
        await pilot.pause()
        await pilot.press("enter")  # под-форма «лёгкая»
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        await pilot.press("enter")  # Enter#1 — пометить Flash
        await pilot.pause()
        assert scr._values["model_light"] == "Flash"
        await pilot.press("enter")  # Enter#2 — назад к списку уровней
        await pilot.pause()
        assert scr._levels_stage == "ask"
        await pilot.press("down")  # → строка «максимальная»
        await pilot.press("enter")
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        await pilot.press("down")  # → Heavy
        await pilot.press("enter")
        await pilot.pause()
        assert scr._values["model_max"] == "Heavy"
        await pilot.press("enter")
        await pilot.pause()
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        await pilot.press("enter")
        await wait_until(pilot, lambda: committed, "commit done")
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert raw["models"] == {
            "Qwen": {"model": "Qwen"},
            "Flash": {"model": "Flash"},
            "Heavy": {"model": "Heavy"},
        }
        assert raw["slots"] == {
            "default": "Qwen",
            "light": "Flash",
            "max": "Heavy",
        }


@pytest.mark.asyncio
async def test_level_form_escape_returns_to_the_list_without_setting(tmp_path):
    """Esc-вложенность второго под-состояния: под-форма уровня закрывается Esc к
    списку уровней, ничего не зафиксировав."""
    pytest.importorskip("textual")
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from gc_tui.screens.confirm import ConfirmScreen
    from gc_tui.screens.onboarding import STEP_LEVELS

    _cfg_path, scr = _levels_screen(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_LEVELS)
        await pilot.pause()
        await pilot.press("enter")
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        await pilot.press("escape")
        await pilot.pause()
        assert scr._levels_stage == "ask"
        assert "model_light" not in scr._values
        assert not isinstance(app.screen, ConfirmScreen)


def test_level_row_paints_the_level_name_with_its_own_color():
    """Имя уровня в строке шага несёт ЦВЕТ уровня из `model_sign.LEVEL_COLORS` (max — RUN_COLOR),
    как в раскрытом списке `/level` и в слотах секции «Модели»: одна сущность не имеет
    права выглядеть на одном экране двумя способами. Роль после «·» — обычная."""
    from gc_tui.palette import INK, RUN_COLOR

    scr = _screen()
    row = scr._level_row_text(False, "max")
    styles = {
        row.plain[span.start : span.end]: span.style for span in row.spans
    }
    assert styles.get("max") == RUN_COLOR
    assert styles.get(" · самая сильная: сложные задачи") == INK


@pytest.mark.asyncio
async def test_model_step_names_the_level_it_fills(tmp_path):
    """Шаг «Модель» задаёт слот `default` — тот же уровень, что и шаг «Уровни».
    Подпись называет связь: без неё слово «default» человек впервые встречает уже
    в итоговой сводке («модель default: …»)."""
    pytest.importorskip("textual")
    from gc_core.model_picker import ModelInfo
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_MODEL

    scr = _screen(
        fetch=lambda *a, **k: [ModelInfo("Flash", tool_capable=True)]
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_MODEL)
        await pilot.pause()
        blob = " ".join(str(w.render()) for w in scr.query("Static.onb-dyn"))
        assert "уровень default" in blob


@pytest.mark.asyncio
async def test_levels_continue_row_leaves_the_step_on_enter(tmp_path):
    """Выход со шага — строкой-действием: Enter на уровне раскрывает форму, и «далее»
    ему взять неоткуда. Курсор ходит по трём строкам (light, max, «продолжить»), и на
    последней Enter уводит на следующий шаг, ничего не записав."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_LEVELS, STEP_MCP

    cfg_path, scr = _levels_screen(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_LEVELS)
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")  # light → max → продолжить
        await pilot.pause()
        assert scr._levels_on_continue()
        await pilot.press("enter")
        await pilot.pause()
        assert scr._step_id() == STEP_MCP
        assert (
            "model_light" not in scr._values and "model_max" not in scr._values
        )


@pytest.mark.asyncio
async def test_levels_cursor_cycles_through_three_rows(tmp_path):
    """Список замкнут по трём строкам: с «продолжить» ↓ возвращает на первый уровень."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_LEVELS

    cfg_path, scr = _levels_screen(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_LEVELS)
        await pilot.pause()
        seen = []
        for _ in range(4):
            seen.append(scr._levels_idx)
            await pilot.press("down")
            await pilot.pause()
        assert seen == [0, 1, 2, 0]


@pytest.mark.asyncio
async def test_levels_footer_follows_the_row_under_the_cursor(tmp_path):
    """Футер обещает роль Enter для строки ПОД КУРСОРОМ: на уровне — «выбрать»
    (далее уводит PgDn), на строке-действии Enter сам и есть «далее»."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_LEVELS
    from textual.widgets import Static

    cfg_path, scr = _levels_screen(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_LEVELS)
        await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render()).replace(
            FOOTER_SCROLL, ""
        )
        assert "Enter — выбрать" in footer and "PgDn — далее" in footer
        await pilot.press("down")
        await pilot.press("down")  # → «продолжить»
        await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render()).replace(
            FOOTER_SCROLL, ""
        )
        assert "Enter — далее" in footer and "PgDn — далее" not in footer


@pytest.mark.asyncio
async def test_levels_footer_promises_the_keys_that_work(tmp_path):
    """Футер шага: в списке уровней Enter выбирает (далее — PgDn), в под-форме
    работают Alt+↓ (перечитать список) и Esc — назад к списку, а не выход."""
    pytest.importorskip("textual")
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from gc_tui.screens.onboarding import STEP_LEVELS
    from textual.widgets import Static

    _cfg_path, scr = _levels_screen(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_LEVELS)
        await pilot.pause()
        footer = str(scr.query_one("#onb-footer", Static).render()).replace(
            FOOTER_SCROLL, ""
        )
        assert "Enter — выбрать" in footer and "PgDn — далее" in footer
        assert "Esc — выход" in footer
        await pilot.press("enter")
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        footer = str(scr.query_one("#onb-footer", Static).render()).replace(
            FOOTER_SCROLL, ""
        )
        assert "Alt+↓ — перечитать список" in footer
        assert "Esc — назад" in footer and "Esc — выход" not in footer


def _screen_with_profiles_on_disk(tmp_path, **over):
    """Мастер на конфиге, рядом с которым просто лежат файлы профилей `home` и
    `work` (указатель `profile` в конфиге не выставлен) — состав шагов мастера
    от их наличия не зависит."""
    cfg_path = _profiles_cfg(tmp_path, "home", "work")
    return cfg_path, _screen(config_path=str(cfg_path), **over)


@pytest.mark.asyncio
async def test_full_pass_through_both_new_steps_writes_valid_config(
    tmp_path, monkeypatch
):
    """Полный проход: подключение → модель → уровень max → MCP → голос → подтверждение.
    Итог — валидный конфиг (та же двухфазная проверка, что у записи реестра)."""
    pytest.importorskip("textual")
    import yaml
    from gc_core.config import validate_config_text_with_sections
    from gc_core.model_picker import ModelInfo
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.connection import ConnectionEditor
    from gc_tui.editors.model import ModelEditor
    from gc_tui.screens.onboarding import (
        STEP_CONFIRM,
        STEP_LEVELS,
        STEP_MCP,
        STEP_MODEL,
        STEP_VOICE,
    )
    from test_onboarding_voice_step import FakeVoice
    from textual.widgets import Input

    monkeypatch.chdir(tmp_path)
    committed = []
    cfg_path, scr = _screen_with_profiles_on_disk(
        tmp_path,
        on_commit=lambda: committed.append(True),
        voice=FakeVoice(),
        fetch=lambda *a, **k: [
            ModelInfo("Flash", tool_capable=True),
            ModelInfo("Heavy", tool_capable=True),
        ],
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        ed = scr.query_one(ConnectionEditor)
        ed.escape_presets()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://h/v1/chat/completions"
        await pilot.press("enter")  # → шаг «Модель»
        await pilot.pause()
        assert scr._step_id() == STEP_MODEL
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        await pilot.press("enter")  # пометить Flash
        await pilot.press("enter")  # → шаг уровней
        await pilot.pause()
        assert scr._step_id() == STEP_LEVELS
        await pilot.press("down")  # строка «максимальная»
        await pilot.press("enter")
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        await pilot.press("down")  # → Heavy
        await pilot.press("enter")
        await pilot.press("enter")  # зафиксировать и вернуться
        await pilot.pause()
        assert scr._step_id() == STEP_LEVELS
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._step_id() == STEP_MCP
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._step_id() == STEP_VOICE
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._step_id() == STEP_CONFIRM
        await pilot.press("enter")
        await wait_until(pilot, lambda: committed, "commit done")
        text = cfg_path.read_text(encoding="utf-8")
        validate_config_text_with_sections(text)  # не бросает
        raw = yaml.safe_load(text)
        assert raw["models"] == {
            "Flash": {"model": "Flash"},
            "Heavy": {"model": "Heavy"},
        }
        assert raw["slots"] == {"default": "Flash", "max": "Heavy"}
        assert raw["profile"] == ""  # указатель не трогали


# --- находки ревью Task 8 ---


@pytest.mark.asyncio
async def test_leaving_the_level_form_does_not_survive_the_step(tmp_path):
    """То же правило для второго под-состояния: под-форма уровня не переживает уход
    со шага — вернувшийся застаёт список уровней."""
    pytest.importorskip("textual")
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.editors.model import ModelEditor
    from gc_tui.screens.onboarding import STEP_LEVELS, STEP_MCP

    _cfg_path, scr = _levels_screen(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_LEVELS)
        await pilot.pause()
        await pilot.press("enter")  # под-форма «лёгкая»
        await wait_until(
            pilot,
            lambda: (
                bool(scr.query(ModelEditor))
                and scr.query(ModelEditor).first().models_loaded
            ),
            "models loaded",
        )
        goto_step(scr, STEP_MCP)
        await pilot.pause()  # ушли со шага мимо под-формы
        await pilot.press("pageup")
        await pilot.pause()
        assert scr._step_id() == STEP_LEVELS
        assert scr._levels_stage == "ask"


# --- действующий профиль: мастер им не управляет, но обязан о нём говорить ---
# Управление профилями живёт в «Настройки · Профили»; мастер только информирует —
# плашкой над телом шага и значениями слоя в витринах.


def _active_profile_screen(tmp_path, **over):
    """Мастер на конфиге с ДЕЙСТВУЮЩИМ профилем `work` (и вторым, `home`, на диске).

    Модель — отличимой строкой, а не односимвольной: витрину подтверждения сверяют
    вхождением, и короткое значение нашлось бы внутри соседнего адреса (`completions`),
    зеленя проверку модели независимо от того, показывает её витрина или нет."""
    cfg_path = _profiles_cfg(tmp_path, "home", "work", active="work")
    kw = dict(
        config_path=str(cfg_path),
        config={
            "backend.completions_url": "https://h/v1/chat/completions",
            "backend.chat_completion_headers": {},
            "agent.model": "profile-layer-model",
            "profile": "work",
        },
    )
    kw.update(over)
    return cfg_path, _screen(**kw)


@pytest.mark.asyncio
async def test_layer_note_in_the_wizard_carries_the_settings_address(
    tmp_path, monkeypatch
):
    """Экран мастера — единственная поверхность, где «Настройки · Профили»
    существуют: общий текст политики он дополняет адресом действия."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    monkeypatch.chdir(tmp_path)
    _cfg_path, scr = _active_profile_screen(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        note = str(scr.query_one("#onb-profile-warn", Static).render())
        assert note == (
            'действует профиль "work" — он перекроет модельные настройки · изменить в Настройки · Профили'
        )


@pytest.mark.asyncio
async def test_no_active_profile_shows_no_warning_plaque(
    tmp_path, monkeypatch
):
    """Профили `home`/`work` лежат на диске, но указателя `profile` в конфиге
    нет — действующего профиля тоже нет, и плашки `#onb-profile-warn` на экране
    быть не должно вовсе (а не пустой строкой)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.css.query import NoMatches

    monkeypatch.chdir(tmp_path)
    cfg_path = _profiles_cfg(tmp_path, "home", "work")
    scr = _screen(config_path=str(cfg_path))
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        with pytest.raises(NoMatches):
            scr.query_one("#onb-profile-warn")


def _broken_profile_layout(tmp_path):
    """База с указателем на профиль, который один задаёт `slots.default` — та
    самая раскладка живого стенда, на которой лестница приземляется на ступень
    `without_profile`: конфиг в памяти уже без профиля, а на диске он ещё есть."""
    base = tmp_path / "config.yml"
    base.write_text(
        'profile: "work"\nmodels:\n  fast:\n    model: q\n', encoding="utf-8"
    )
    profile = tmp_path / "profiles" / "work.yml"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text('slots:\n  default: "strong"\n', encoding="utf-8")
    return base


def test_layer_note_is_silent_about_the_profile_that_will_be_deactivated(
    tmp_path,
):
    """Финал мастера деактивирует профиль — значит, перекрыть выбор человека он
    уже не сможет. Плашка «этот слой его перекроет» судит по диску, где профиль
    ещё лежит, и на одном экране с предупреждением о деактивации прямо ему
    противоречила (живой смок)."""
    base = _broken_profile_layout(tmp_path)
    scr = _screen(
        config={"profile": "", "models": {"fast": {}}},
        config_path=str(base),
        deactivate_profile=True,
    )
    assert scr._layer_note() is None


def test_layer_note_still_warns_about_the_profile_without_deactivation(
    tmp_path,
):
    """Сторож: без деактивации профиль на диске действительно перекроет запись —
    плашка обязана остаться."""
    base = _broken_profile_layout(tmp_path)
    scr = _screen(
        config={"profile": "", "models": {"fast": {}}},
        config_path=str(base),
        deactivate_profile=False,
    )
    note = scr._layer_note()
    assert note is not None and "реестр моделей" in note


def test_layer_note_keeps_warning_about_a_project_layer_despite_deactivation(
    tmp_path, monkeypatch
):
    """Деактивация снимает ТОЛЬКО профиль. Проектный `.gc/config.yml` продолжит
    перекрывать реестр — про него плашка молчать не имеет права.

    Профиль правит ТЕ ЖЕ ключи, что и проектный слой, — самая дорогая раскладка:
    происхождение называет лишь ПОСЛЕДНИЙ перекрывший слой, поэтому профиль,
    ложась сверху, заслоняет собой проектный. Отбор по виду слоя («исключить
    профильные origin'ы») выбрасывал их обоих, плашка молчала — а человек
    выбирал модель, мастер писал её в базу и снимал профиль, после чего молча
    побеждал слот из проектного слоя."""
    from gc_core.config_modules import env_vars

    base = tmp_path / "config.yml"
    base.write_text(
        'profile: "work"\nmodels:\n  base:\n    model: b\nslots:\n  default: base\n',
        encoding="utf-8",
    )
    profile = tmp_path / "profiles" / "work.yml"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        "models:\n  wk:\n    model: w\nslots:\n  default: wk\n",
        encoding="utf-8",
    )
    project = tmp_path / ".gc" / "config.yml"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text(
        "models:\n  proj:\n    model: p\nslots:\n  default: proj\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        env_vars, "project_local_config_path", lambda: str(project)
    )
    scr = _screen(
        config={"profile": "", "models": {"proj": {}}},
        config_path=str(base),
        deactivate_profile=True,
    )
    note = scr._layer_note()
    assert note is not None and "реестр моделей" in note


def test_registry_layer_note_has_no_settings_address(tmp_path, monkeypatch):
    """Второй вердикт `overriding_layer_note` — про реестр из чужого слоя, и
    «Настройки · Профили» к нему отношения не имеют: хвост клеится только к
    профильной ветке (спека §3)."""
    from gc_core.config_modules import env_vars

    base = tmp_path / "config.yml"
    base.write_text('profile: ""\n', encoding="utf-8")
    project = tmp_path / ".gc" / "config.yml"
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text(
        "models:\n  proj:\n    model: p\nslots:\n  default: proj\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        env_vars, "project_local_config_path", lambda: str(project)
    )
    scr = _screen(config={"models": {"proj": {}}}, config_path=str(base))
    note = scr._layer_note()
    assert note is not None and "реестр моделей" in note
    assert "Настройки · Профили" not in note


@pytest.mark.asyncio
async def test_layer_note_shows_on_every_step_for_an_active_profile(
    tmp_path, monkeypatch
):
    """Информирование остаётся: активный профиль виден на каждом шаге и на
    подтверждении (спека §3)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM, STEP_MODEL
    from textual.widgets import Static

    monkeypatch.chdir(tmp_path)
    _cfg_path, scr = _active_profile_screen(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        for step in (STEP_MODEL, STEP_CONFIRM):
            goto_step(scr, step)
            await pilot.pause()
            note = str(scr.query_one("#onb-profile-warn", Static).render())
            assert 'действует профиль "work"' in note


@pytest.mark.asyncio
async def test_degraded_config_note_shows_on_every_step(tmp_path, monkeypatch):
    """Сообщение о деградации загрузки видно НА ЭКРАНЕ мастера, на каждом шаге.

    Единственный прежний адрес показа — рантайм-уведомление — под мастером
    недостижим: `BLOCKS_GLOBAL_NAV` намеренно гасит F4, и текст всплывал уже
    ПОСЛЕ финала, когда устаревал, а при выходе по Esc — не всплывал никогда.
    Человек не видел ни строчки и не понимал, почему поля мастера выглядят иначе,
    чем его файл (живой смок)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import (
        STEP_CONFIRM,
        STEP_CONNECTION,
        STEP_MODEL,
    )
    from textual.widgets import Static

    monkeypatch.chdir(tmp_path)
    message = 'config.yml не читается с профилем "work" (алиас "strong" не найден) — профиль снят на этот запуск'
    cfg_path = _profiles_cfg(tmp_path, "work")
    scr = _screen(config_path=str(cfg_path), degraded_note=message)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        for step in (STEP_CONNECTION, STEP_MODEL, STEP_CONFIRM):
            goto_step(scr, step)
            await pilot.pause()
            assert message in str(
                scr.query_one("#onb-degraded-warn", Static).render()
            )


@pytest.mark.asyncio
async def test_healthy_load_shows_no_degradation_note(tmp_path, monkeypatch):
    """Отрицательный сторож: конфиг собрался целиком — плашке о деградации на
    экране взяться неоткуда."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    monkeypatch.chdir(tmp_path)
    cfg_path = _profiles_cfg(tmp_path, "work")
    scr = _screen(config_path=str(cfg_path))
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert len(scr.query("#onb-degraded-warn")) == 0


@pytest.mark.asyncio
async def test_confirm_shows_the_profile_layer_values(tmp_path, monkeypatch):
    """Новое поведение (спека §5): при активном профиле витрина показывает
    значения ЕГО слоя как значения конфига — это правда о том, чем агент будет
    работать, а плашка рядом объясняет их происхождение."""
    pytest.importorskip("textual")
    import re

    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM

    monkeypatch.chdir(tmp_path)
    # конфиг слоя: url https://h/…, model profile-layer-model
    _cfg_path, scr = _active_profile_screen(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        # Витрина подтверждения — тело шага (`.onb-dyn`), а не заголовочный
        # `#onb-body`: тот несёт только имя шага.
        body = " ".join(str(w.render()) for w in scr.query("Static.onb-dyn"))
        flat = re.sub(r"\s+", " ", body)  # колонка меток — не предмет теста
        # Строки витрины целиком: значение обязано стоять ЗА своей меткой, иначе
        # проверка зеленела бы от случайного вхождения в соседнюю строку.
        assert "адрес: https://h/v1/chat/completions" in flat
        assert "модель default: profile-layer-model" in flat
        assert "профиль:" not in flat  # строка выбора ушла вместе с шагом


def test_stepper_marks_match_the_position(tmp_path, monkeypatch):
    """Полоса шагов честна по построению: на шаге N ровно N−1 «✓» и len−N «○»
    (прыжок через голову, который её ломал, снят)."""
    monkeypatch.chdir(tmp_path)
    _cfg_path, scr = _active_profile_screen(tmp_path)
    total = scr._last_step()
    for position in range(total):
        scr._step = position
        text = str(scr._stepper_text())
        assert text.count("✓") == position
        assert text.count("○") == total - position - 1
    # Финал в полосе не показан вовсе, но остальные шаги на нём пройдены — иначе
    # подтверждение выглядело бы серединой пути.
    scr._step = total
    text = str(scr._stepper_text())
    assert text.count("✓") == total
    assert "○" not in text


def test_wizard_checks_token_on_the_real_route():
    """Мастер не должен писать «доступен» по публичной models-цепочке: у
    OpenRouter она отвечает без токена, а chat/completions его требует. Считаем
    вызовы — контроль без токена и повтор с ним, а не один запрос."""
    from gc_tui.services.settings_probe import ProbeResult

    seen = []

    def fake_auth(
        endpoint, auth, verify, model, *, config_proxy=None, post_ex=None
    ):
        seen.append(auth)
        if auth is None:
            return ProbeResult(
                "rejected", 401, None, 5, "токен отклонён (401)"
            )
        return ProbeResult("ok", 200, None, 5, "доступен")

    screen = _screen(probe=fake_auth)
    result = screen._run_connection_probe(
        "http://x/v1/chat/completions", "Bearer good", True, None
    )
    assert seen == [None, "Bearer good"]
    assert result.auth_state == "accepted" and "токен принят" in result.detail


# ── Ctrl+U и автопроба шага «Подключение» (разбор ревью 2026-08-18) ──


def _fake_auth_probe(seen=None):
    """Заглушка с сигнатурой `probe_auth`: сети нет, вызовы можно считать."""
    from gc_tui.services.settings_probe import ProbeResult

    def probe(
        endpoint, auth, verify, model, *, config_proxy=None, post_ex=None
    ):
        if seen is not None:
            seen.append(auth)
        return ProbeResult("ok", 200, None, 1, "доступен")

    return probe


async def _conn_step(pilot, tmp_path, **over):
    """Мастер на шаге «Подключение» поверх конфига с СОХРАНЁННЫМ токеном."""
    cfg_path = _write_conn_cfg(
        tmp_path,
        url="https://h/v1/chat/completions",
        auth="Bearer sk-SECRET",
        model="m",
    )
    cfg = _resolved_conn_cfg(cfg_path)
    kw = dict(
        review_all=True,
        config_path=str(cfg_path),
        config=cfg,
        probe=_fake_auth_probe(),
    )
    kw.update(over)
    scr = _screen(**kw)
    await pilot.app.push_screen(scr)
    await pilot.pause()
    from gc_tui.editors.connection import ConnectionEditor

    ed = scr.query_one(ConnectionEditor)
    ed.escape_presets()
    return scr, ed


def _count_probes(scr):
    """Считает фактические пробы, не подменяя их: снимок пишет сам `_auto_probe`."""
    calls = []
    orig = scr._auto_probe

    def counted():
        calls.append(1)
        orig()

    scr._auto_probe = counted
    return calls


@pytest.mark.asyncio
async def test_wizard_ctrl_u_on_token_clears_saved_token(tmp_path):
    """Ctrl+U на поле токена снимает сохранённый — та же клавиша, что в /settings.

    Ветка обработчика читала `ConnectionEditor` раньше функционально-локальных
    импортов ниже по той же `on_key`: имя было локальным для всей функции, и
    первое же нажатие роняло мастер (`except NoMatches` этого не ловит)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr, ed = await _conn_step(pilot, tmp_path)
        ed.move(1)  # лента курсора: адрес → токен
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert ed.token_cleared is True


@pytest.mark.asyncio
async def test_wizard_ctrl_u_on_address_field_leaves_token_alone(tmp_path):
    """Ctrl+U — клавиша поля токена (футер обещает её только там). На адресе она
    молчит: иначе сохранённый токен помечается к удалению незаметно для того, кто
    целился очистить строку адреса."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr, ed = await _conn_step(pilot, tmp_path)
        await pilot.press("ctrl+u")  # курсор на адресе (позиция 0)
        await pilot.pause()
        assert ed.token_cleared is False


@pytest.mark.asyncio
async def test_wizard_arrows_without_edits_do_not_reprobe(tmp_path):
    """Ходьба по форме без правок не стучится в сервер.

    Гейт по снимку был только у ветки адреса: уход с токена/прокси звал пробу
    безусловно, и пролистанная стрелками форма давала запрос на каждое нажатие
    (на платном провайдере — деньги)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr, ed = await _conn_step(pilot, tmp_path)
        calls = _count_probes(scr)
        await pilot.press(
            "down"
        )  # уход с адреса — первая (и единственная) проба
        await pilot.press("down")
        await pilot.press("up")
        await pilot.press("up")
        await pilot.pause()
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_wizard_reprobes_after_token_cleared(tmp_path):
    """Очистка токена — изменение того, что уйдёт в запрос, значит пробу надо
    повторить. Снимок хранил только (адрес, набранный токен, прокси): Ctrl+U его
    не двигал, и на экране оставался вердикт «токен принят» под конфигурацией,
    где токена уже нет."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr, ed = await _conn_step(pilot, tmp_path)
        calls = _count_probes(scr)
        await pilot.press("down")  # уход с адреса → проба #1
        await pilot.press("ctrl+u")  # курсор на токене — снять сохранённый
        await pilot.press("down")  # уход с токена → проба обязана повториться
        await pilot.pause()
        assert ed.token_cleared is True
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_review_levels_step_shows_the_levels_already_assigned(tmp_path):
    """Тот же промах, что чинили на шаге «Модель», — на под-форме
    уровней: она читала ТОЛЬКО сессионные `_values`, и на повторном заходе все уровни
    показывались «не задана», даже заданные в файле. Человек переназначал их заново, не
    видя, что там уже стоит.

    Показывается ЯВНО назначенный слот, а не резолв матрицей фоллбэков: незаданный
    `max` работает моделью `default`, но написать против него её имя значило бы сказать
    «уровень назначен» там, где он не назначен."""
    from gc_core.model_picker import ModelInfo
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_LEVELS

    cfg_path, cfg = _registry_cfg(tmp_path, "z-ai/glm-5.3", alias="glm-5.3")
    import yaml
    from gc_core.config_modules.registry_write import apply_registry_ops

    apply_registry_ops(
        cfg_path,
        [
            ("upsert_model", "gemini", {"model": "google/gemini-3.7"}),
            ("write_slot", "light", "gemini"),
        ],
        "",
    )
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg["models"], cfg["slots"] = raw.get("models"), raw.get("slots")

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            review_all=True,
            config_path=str(cfg_path),
            config=cfg,
            fetch=lambda *a, **k: [
                ModelInfo("z-ai/glm-5.3", tool_capable=True),
                ModelInfo("google/gemini-3.7", tool_capable=True),
            ],
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_LEVELS)
        await pilot.pause()
        rows = [str(w.render()) for w in scr.query(".onb-option") if w.display]
        light = [r for r in rows if "light" in r]
        assert light and "google/gemini-3.7" in light[0], (
            f"назначенный light не назван: {rows}"
        )
        # max в файле НЕ задан — остаётся «не задана», а не именем модели фоллбэка:
        # незаданный уровень работает моделью default, но назначенным от этого не стал
        max_row = [r for r in rows if "max" in r]
        assert max_row and "не задана" in max_row[0], max_row


@pytest.mark.asyncio
async def test_commit_does_not_claim_an_untouched_config_after_the_registry_landed(
    tmp_path, monkeypatch
):
    """Записей две, и реестр идёт первым (политика в `onboarding.write_wizard_values`).
    Отказ ВТОРОЙ оставляет записи моделей на диске — «конфиг не записан» тогда
    отправляет человека искать нетронутый файл, где уже лежит новый реестр.

    Ветка `missing` рядом это правило учитывает, ветка отказа — нет. Отказ
    инъецируется у самой плоской записи, чтобы политика отработала настоящая."""
    pytest.importorskip("textual")
    from gc_core import onboarding_config_writer
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    cfg_path = _write_conn_cfg(
        tmp_path,
        url="http://host/v1/chat/completions",
        auth="Bearer t",
        model="m",
    )
    monkeypatch.setattr(
        onboarding_config_writer,
        "write_values",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("схема не валидна")),
    )

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            review_all=True,
            config_path=str(cfg_path),
            config=_resolved_conn_cfg(cfg_path),
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._values["model"] = (
            "qwen/qwen3-coder"  # запись реестра — первая транзакция
        )

        assert scr._commit_config() is False

        import yaml

        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert raw["models"] == {"qwen3-coder": {"model": "qwen/qwen3-coder"}}
        body = str(scr.query_one("#onb-body", Static).render())
        assert "схема не валидна" in body
        assert "конфиг не записан" not in body
        assert "модел" in body


@pytest.mark.asyncio
async def test_commit_reports_an_oserror_instead_of_crashing(
    tmp_path, monkeypatch
):
    """Близнец соседнего ValueError-кейса (ревью L33): OSError записи (диск,
    права, win-лок открытого файла) уходил мимо перехвата — падал весь Textual.
    «✗ конфиг не записан» — тот же честный отказ, что и на ValueError: реестра
    здесь нет, не легло ничего."""
    pytest.importorskip("textual")
    from gc_core import onboarding_config_writer
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    cfg_path = _write_conn_cfg(
        tmp_path,
        url="http://host/v1/chat/completions",
        auth="Bearer t",
        model="m",
    )
    monkeypatch.setattr(
        onboarding_config_writer,
        "write_values",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("нет прав на файл")),
    )

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            review_all=True,
            config_path=str(cfg_path),
            config=_resolved_conn_cfg(cfg_path),
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._values["Authorization"] = (
            "Bearer sk"  # только плоская запись, реестра нет
        )

        assert scr._commit_config() is False
        body = str(scr.query_one("#onb-body", Static).render())
        assert "конфиг не записан" in body
        assert "нет прав на файл" in body


@pytest.mark.asyncio
async def test_commit_names_the_deactivation_when_only_the_pointer_landed(
    tmp_path, monkeypatch
):
    """Та же ложь, что чинил плоский мастер: на ступени без профиля указатель
    снимается ПЕРВЫМ и в откате не участвует. Отказ дальше по цепочке печатал
    «✗ конфиг не записан» — неправда, профиль уже деактивирован, и человек уверен,
    что он активен, пока следующий старт не пойдёт без него.

    Что легло, экран читает у писателя (`PartialWriteError.written`), а не выводит
    из его порядка: реестра здесь нет вовсе, и «записи моделей уже в конфиге» было
    бы второй ложью на той же строке."""
    pytest.importorskip("textual")
    import yaml
    from gc_core import onboarding_config_writer
    from gc_core import onboarding_fields as _of
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    cfg_path = _write_conn_cfg(tmp_path, url="http://host/v1/chat/completions")
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8").replace(
            'profile: ""', 'profile: "work"'
        ),
        encoding="utf-8",
    )
    profile_file = tmp_path / "profiles" / "work.yml"
    profile_file.parent.mkdir(parents=True, exist_ok=True)
    profile_file.write_text('slots:\n  default: "strong"\n', encoding="utf-8")
    real_write_values = onboarding_config_writer.write_values

    def refusing_flat_write(path, values, **kwargs):
        if "profile" in values:  # снятие указателя — настоящее
            return real_write_values(path, values, **kwargs)
        raise ValueError("схема не валидна")

    monkeypatch.setattr(
        onboarding_config_writer, "write_values", refusing_flat_write
    )

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(
            review_all=True,
            config_path=str(cfg_path),
            config=_resolved_conn_cfg(cfg_path),
            deactivate_profile=True,
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._values["Authorization"] = (
            "Bearer sk"  # модели нет — реестр не пишется
        )

        assert scr._commit_config() is False

        assert (
            yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["profile"]
            == ""
        )
        body = str(scr.query_one("#onb-body", Static).render())
        assert "схема не валидна" in body
        assert _of.LANDED_WORDS["profile_pointer"] in body
        assert _of.LANDED_WORDS["registry"] not in body
        assert "конфиг не записан" not in body


@pytest.mark.asyncio
async def test_both_wizards_name_the_landed_writes_from_one_place(
    tmp_path, monkeypatch
):
    """Инвариант межповерхностный, поэтому и тест один на оба мастера (тот же приём,
    что у предупреждения о деактивации): об одном и том же событии они обязаны
    говорить одними словами, а у каждого в отдельности своя копия выглядела бы
    исправной."""
    pytest.importorskip("textual")
    from gc_core import onboarding
    from gc_core import onboarding_fields as _of

    # gc-tui от gc-cli НЕ зависит: тест межповерхностный, и в наборе одного
    # пакета он законен только как условный. importorskip, а не import:
    # автономный прогон `cd packages/tui && pytest` обязан проходить БЕЗ
    # соседнего дистрибутива, иначе зелёный цвет означал бы лишь то, что
    # сосед оказался в общем окружении (см. tests/test_test_ownership.py).
    wz = pytest.importorskip("gc_cli.onboarding_wizard")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    sentinel = "СЛОВО-ИЗ-ОБЩЕГО-ДОМА"
    monkeypatch.setattr(_of, "LANDED_WORDS", {"registry": sentinel})

    def refusing_after_the_registry(
        config_path, values, *, deactivate_profile=False
    ):
        raise onboarding.PartialWriteError(
            "схема не валидна", written=("registry",)
        )

    monkeypatch.setattr(
        onboarding, "write_wizard_values", refusing_after_the_registry
    )

    printed: list = []
    monkeypatch.setattr(wz, "_reload_config", make_settings)
    # Ридер отвечает URL только на ПЕРВЫЙ вопрос (адрес), дальше — Enter.
    # Константа здесь не годится: шаг выбора модели трактует непустой
    # несовпадающий ответ как новый запрос (см. `_ask_as_is`), и мастер честно
    # переспрашивает — виток за витком. Раньше это сходило с рук случайно:
    # список моделей не приходил, и шаг выбора не открывался вовсе. Autouse
    # `_no_network_model_fetch` подаёт список — шаг открылся, и константный
    # ридер положил процесс (4,3 ГБ печати, SIGKILL). Тесту выбор модели не
    # нужен: он про то, что оба мастера называют записанное одними словами.
    answers = iter(["https://h/v1/chat/completions"])

    def _reader(prompt, secret):
        return next(answers, "")

    # Незаполненные адрес и модель — плейсхолдеры шаблона (в загруженном
    # конфиге оба ключа есть всегда).
    wz.run(
        make_settings(
            {
                "backend.completions_url": onboarding.URL_SENTINEL,
                "agent.model": onboarding.MODEL_SENTINEL,
            }
        ),
        tmp_path / "nowhere.yml",
        review_all=False,
        io=wz.WizardIO(reader=_reader, printer=printed.append),
    )
    assert any(sentinel in line for line in printed)

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert scr._commit_config() is False
        assert sentinel in str(scr.query_one("#onb-body", Static).render())


# --- ступень «без профиля»: мастер деактивирует профиль (спека починки) ------


@pytest.mark.asyncio
async def test_confirm_step_warns_that_the_profile_will_be_deactivated(
    tmp_path,
):
    """Enter на подтверждении снимет указатель профиля — человек обязан узнать об
    этом ДО нажатия, а не обнаружить постфактум. Текст берётся из общего дома."""
    pytest.importorskip("textual")
    from gc_core import onboarding_fields as _of
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(deactivate_profile=True)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        blob = " ".join(str(w.render()) for w in scr.query("Static.onb-dyn"))
        assert _of.PROFILE_DEACTIVATION_WARNING in blob


@pytest.mark.asyncio
async def test_confirm_step_says_nothing_about_the_profile_on_a_healthy_config(
    tmp_path,
):
    """Отрицательный сторож: конфиг собрался целиком (ступень `full`) — мастер
    профиля не касается, и обещать его снятие нельзя."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen()  # признак не передан — ступень не «без профиля»
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        blob = " ".join(str(w.render()) for w in scr.query("Static.onb-dyn"))
        assert "деактив" not in blob.lower()


@pytest.mark.asyncio
async def test_confirm_commits_the_deactivation_along_with_the_settings(
    tmp_path,
):
    """Финал доводит признак до записи: указатель снят, файл профиля цел,
    настройки в файле. Без этого мастер доходил до конца и не мог записать
    собственную починку — гейт судит стек следующего старта, а там профиль."""
    pytest.importorskip("textual")
    import yaml
    from gc_testkit.tui_support import wait_until
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM

    committed = []
    cfg_path, scr = _levels_screen(
        tmp_path,
        on_commit=lambda: committed.append(True),
        deactivate_profile=True,
    )
    text = cfg_path.read_text(encoding="utf-8").replace(
        'profile: ""', 'profile: "work"'
    )
    cfg_path.write_text(text, encoding="utf-8")
    profile_file = tmp_path / "profiles" / "work.yml"
    profile_file.parent.mkdir(parents=True, exist_ok=True)
    profile_file.write_text('slots:\n  default: "strong"\n', encoding="utf-8")
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        await pilot.press("enter")
        await wait_until(pilot, lambda: committed, "commit done")
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert raw["profile"] == ""  # указатель снят
        assert profile_file.is_file()  # …а файл профиля цел
        assert raw["slots"]["default"] == "Qwen"  # настройки записаны


@pytest.mark.asyncio
async def test_both_wizards_take_the_deactivation_warning_from_one_place(
    tmp_path, monkeypatch
):
    """Инвариант межповерхностный, поэтому и тест один на оба мастера: у каждого
    в отдельности своя копия текста выглядела бы исправной. Подменяем ЕДИНСТВЕННЫЙ
    дом текста и ждём подмену в обоих."""
    pytest.importorskip("textual")
    from gc_core import onboarding_fields as _of

    # gc-tui от gc-cli НЕ зависит: тест межповерхностный, и в наборе одного
    # пакета он законен только как условный. importorskip, а не import:
    # автономный прогон `cd packages/tui && pytest` обязан проходить БЕЗ
    # соседнего дистрибутива, иначе зелёный цвет означал бы лишь то, что
    # сосед оказался в общем окружении (см. tests/test_test_ownership.py).
    wz = pytest.importorskip("gc_cli.onboarding_wizard")
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import STEP_CONFIRM

    sentinel = "СТРОКА-ИЗ-ОБЩЕГО-ДОМА"
    monkeypatch.setattr(_of, "PROFILE_DEACTIVATION_WARNING", sentinel)

    printed: list = []
    monkeypatch.setattr(wz, "_reload_config", make_settings)
    wz.run(
        make_settings({}),
        tmp_path / "nowhere.yml",
        review_all=False,
        io=wz.WizardIO(reader=lambda p, s: "", printer=printed.append),
        deactivate_profile=True,
    )
    assert any(sentinel in line for line in printed)

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        scr = _screen(deactivate_profile=True)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        goto_step(scr, STEP_CONFIRM)
        await pilot.pause()
        blob = " ".join(str(w.render()) for w in scr.query("Static.onb-dyn"))
        assert sentinel in blob
