from gc_testkit.tui_support import as_settings, tui_settings
import pytest
from screen_text import config_text


def test_profile_header_hides_unset_model_sentinel():
    """Task 14: шаблонный сентинел `agent.model` — не имя модели, шапка /config
    не должна показывать его как значение (см. onboarding.MODEL_SENTINEL)."""
    from gc_core import onboarding
    from gc_tui.screens.config import _profile_header_text

    cfg = {"agent.model": onboarding.MODEL_SENTINEL}
    text = _profile_header_text(as_settings(cfg), profile_source=None).plain
    assert onboarding.MODEL_SENTINEL not in text


def test_profile_header_shows_registry_model_not_flat_key():
    """Шапка называет ДЕЙСТВУЮЩУЮ модель — резолв Router'а (§6/§7), как футер и
    /status. С волны B мастер плоский ключ не пишет вовсе, и чтение `cfg["model"]`
    показывало бы прочерк рядом с полностью настроенным реестром."""
    from gc_core import onboarding
    from gc_tui.screens.config import _profile_header_text

    cfg = {
        "agent.model": onboarding.MODEL_SENTINEL,
        "level": "default",
        "models": {"flash": {"model": "google/gemini-2.5-flash"}},
        "slots": {"default": "flash"},
    }
    text = _profile_header_text(as_settings(cfg), profile_source=None).plain
    assert "модель: flash" in text


def test_profile_header_prefers_resolved_model_over_stale_flat_key():
    """Худший случай плоского чтения: `agent.model` заполнен руками и устарел —
    шапка называла бы моделью то, чем работа не идёт."""
    from gc_tui.screens.config import _profile_header_text

    cfg = {
        "agent.model": "stale-legacy-model",
        "level": "default",
        "models": {"flash": {"model": "google/gemini-2.5-flash"}},
        "slots": {"default": "flash"},
    }
    text = _profile_header_text(as_settings(cfg), profile_source=None).plain
    assert "stale-legacy-model" not in text
    assert "модель: flash" in text


def test_profile_header_falls_back_to_flat_model_in_legacy_mode():
    """Пустой реестр — легаси-резолв, плоский ключ остаётся именем модели."""
    from gc_tui.screens.config import _profile_header_text

    text = _profile_header_text(
        as_settings({"agent.model": "qwen3-coder"}), profile_source=None
    ).plain
    assert "модель: qwen3-coder" in text


def test_profile_header_resolves_auto_level_to_default_slot_model():
    """level: auto (спека auto-routing §5.1) переводится через `effective_target`
    ДО Router'а — сырой литерал иначе роняет резолв (ValueError), и шапка
    показала бы фоллбэк вместо модели default-слота. Строка «уровень:» —
    литерал `auto` при доступном автовыборе (как /status), иначе действующий
    уровень."""
    from gc_tui.screens.config import _profile_header_text

    cfg = {
        "agent.model": "m",
        "level": "auto",
        "backend.api_mode": "chat_completions",
        "agent.auto_routing": True,
        "models": {"fast": {}, "mid": {}, "big": {}},
        "slots": {"light": "fast", "default": "mid", "max": "big"},
    }
    text = _profile_header_text(as_settings(cfg), profile_source=None).plain
    assert "уровень: auto" in text
    assert "модель: mid" in text


@pytest.mark.asyncio
async def test_config_screen_yaml_redacts_secret_and_shows_paths(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    cfg = {
        "agent.model": "M",
        "backend.chat_completion_headers": {"Authorization": "Bearer sk-REAL"},
        "paths.agent_md_path": "/p/agent.md",
        "paths.threads_data_path": "/p/threads",
        "paths.logs_dump_dir": "/p/logs",
        "mcp.servers": {},
    }
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    app._config_path = "/p/config.yml"
    async with app.run_test() as pilot:
        from gc_tui.screens.config import ConfigScreen

        scr = ConfigScreen(
            settings=app.base_settings(), config_path=app._config_path
        )
        await app.push_screen(scr)
        await pilot.pause()
        text = config_text(scr)  # helper: весь YAML + пути одной строкой
        assert "sk-REAL" not in text  # секрет замаскирован
        assert "***" in text
        assert "/p/config.yml" in text  # путь конфига
        assert "/p/agent.md" in text  # agent.md
        assert "project_memory.md" in text  # строка про project memory


@pytest.mark.asyncio
async def test_command_config_opens_screen(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.config import ConfigScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._config_path = "/p/config.yml"
    async with app.run_test() as pilot:
        await pilot.app.screen._handle_command("config")
        await pilot.pause()
        assert isinstance(app.screen, ConfigScreen)


@pytest.mark.asyncio
async def test_config_screen_esc_returns_to_main(tmp_path):
    # Esc должен возвращать на главный экран, а не выходить из TUI.
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.config import ConfigScreen
    from gc_tui.screens.main import TraceScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._config_path = "/p/config.yml"
    async with app.run_test() as pilot:
        await pilot.app.screen._handle_command("config")
        await pilot.pause()
        assert isinstance(app.screen, ConfigScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, TraceScreen)


@pytest.mark.asyncio
async def test_config_screen_has_header_and_footer(tmp_path):
    # Консистентность с прочими экранами: есть #header и #footer.
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.config import ConfigScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._config_path = "/p/config.yml"
    async with app.run_test() as pilot:
        scr = ConfigScreen(
            settings=app.base_settings(), config_path=app._config_path
        )
        await app.push_screen(scr)
        await pilot.pause()
        assert scr.query("#header") and scr.query("#footer")


# ---- fieldset-панели (spec 2026-06-10) ----


def test_paths_block_is_rich_text_without_pseudo_comment():
    from gc_tui.screens.config import _paths_block
    from rich.text import Text

    t = _paths_block(
        as_settings({"paths.agent_md_path": "/p/agent.md"}),
        "/p/config.yml",
        None,
    )
    assert isinstance(t, Text)
    plain = t.plain
    assert "# Пути" not in plain  # псевдокомментарий ушёл в border_title
    assert "config.yml:" in plain and "/p/config.yml" in plain
    assert "agent.md:" in plain and "/p/agent.md" in plain
    assert "project_memory.md:" in plain
    assert "(не задано)" in plain  # threads/logs не заданы
    assert len(t.spans) > 0  # метки подкрашены (dim)


def test_paths_block_project_local_line():
    from gc_tui.screens.config import _paths_block

    plain = _paths_block(
        as_settings({}), "/p/config.yml", "/p/.gc/config.yml"
    ).plain
    assert "+ project-local:" in plain and "/p/.gc/config.yml" in plain


@pytest.mark.asyncio
async def test_config_screen_fieldset_panels(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.config import ConfigScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._config_path = "/p/config.yml"
    async with app.run_test() as pilot:
        scr = ConfigScreen(
            settings=app.base_settings(), config_path=app._config_path
        )
        await app.push_screen(scr)
        await pilot.pause()
        panels = list(scr.query(".config-panel"))
        assert len(panels) == 2
        assert panels[0].border_title == "пути"
        assert panels[0].border_subtitle == "правка внешним редактором"
        assert panels[1].border_title == "config.yml"
        assert panels[1].border_subtitle == "секреты скрыты"


@pytest.mark.asyncio
async def test_config_screen_snapshot_hint_caption(tmp_path):
    # Подсказка-снимок: вторая строка трейса (первая — шапка профиль/уровень/модель,
    # спека §12.3a) и присутствует в тексте экрана (`config_text`).
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.config import _SNAPSHOT_HINT, ConfigScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._config_path = "/p/config.yml"
    async with app.run_test() as pilot:
        scr = ConfigScreen(
            settings=app.base_settings(), config_path=app._config_path
        )
        await app.push_screen(scr)
        await pilot.pause()
        trace = scr.query_one("#trace")
        first, second = trace.children[0], trace.children[1]
        assert first.has_class("config-caption")
        assert second.has_class("config-caption")
        assert "после перезапуска" in _SNAPSHOT_HINT
        assert _SNAPSHOT_HINT in config_text(scr)


@pytest.mark.asyncio
async def test_config_trace_quiet_focus_keeps_keyboard_scroll(tmp_path):
    # outline глушится классом quiet-focus, но контейнер ОСТАЁТСЯ фокусируемым —
    # иначе на экране без фокусируемых детей умрёт клавиатурная прокрутка.
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.config import ConfigScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._config_path = "/p/config.yml"
    async with app.run_test() as pilot:
        scr = ConfigScreen(
            settings=app.base_settings(), config_path=app._config_path
        )
        await app.push_screen(scr)
        await pilot.pause()
        trace = scr.query_one("#trace")
        assert trace.has_class("quiet-focus")
        assert trace.can_focus is True


def test_quiet_focus_and_panel_rules_in_tcss():
    # Sync-тест на CSS (прецедент: BRAND_RULE): правила существуют в styles.tcss.
    from pathlib import Path

    import gc_tui as tui

    tcss = (Path(tui.__file__).parent / "styles.tcss").read_text(
        encoding="utf-8"
    )
    assert "#trace.quiet-focus:focus" in tcss
    assert "outline: none" in tcss
    assert ".config-panel" in tcss


# ---- фильтр по ключам конфига (2026-06-26) ----


def test_filter_config_by_key_matches_key_keeps_subtree():
    from gc_tui.screens.config import _filter_config_by_key

    cfg = {"agent.model": "M", "mcp.servers": {"srv": {"transport": "http"}}}
    assert _filter_config_by_key(cfg, "mcp") == {
        "mcp.servers": {"srv": {"transport": "http"}}
    }


def test_filter_config_by_key_nested_keeps_path():
    from gc_tui.screens.config import _filter_config_by_key

    cfg = {"a": {"chat_token": 1, "b": 2}, "x": 9}
    assert _filter_config_by_key(cfg, "token") == {"a": {"chat_token": 1}}


def test_filter_config_by_key_no_match_empty():
    from gc_tui.screens.config import _filter_config_by_key

    assert _filter_config_by_key({"model": "M"}, "zzz") == {}


def test_filter_config_by_key_case_insensitive():
    from gc_tui.screens.config import _filter_config_by_key

    assert _filter_config_by_key({"Model": "M"}, "model") == {"Model": "M"}


def test_filtered_yaml_text_filters_full_and_no_match():
    from gc_tui.screens.config import ConfigScreen

    scr = ConfigScreen(
        settings=as_settings(
            {
                "agent.model": "M",
                "mcp.servers": {"s": {"transport": "http", "url": "http://x"}},
            }
        )
    )
    scr._filter = "mcp"
    txt = scr._filtered_yaml_text()
    # Снимок — документом, как config.yml: секция `mcp` с `servers`.
    assert "servers:" in txt and "model" not in txt
    scr._filter = "zzz"
    assert scr._filtered_yaml_text() == "(нет совпадений)"
    scr._filter = ""
    assert "model" in scr._filtered_yaml_text()  # пустой → полный конфиг


@pytest.mark.asyncio
async def test_config_filter_input_focused_and_filters(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.config import ConfigScreen
    from textual.widgets import Input

    cfg = {"agent.model": "M", "mcp.servers": {"srv": {"transport": "http"}}}
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings(cfg)
    )
    app._config_path = "/p/config.yml"
    async with app.run_test() as pilot:
        scr = ConfigScreen(
            settings=app.base_settings(), config_path=app._config_path
        )
        await app.push_screen(scr)
        await pilot.pause()
        flt = scr.query_one("#config-filter", Input)
        assert flt.has_focus  # автофокус (конвенция со скиллами)
        flt.value = "mcp"
        await pilot.pause()
        assert scr._filter == "mcp"
        # блок путей остаётся при активном фильтре (фильтруем только YAML-панель)
        assert any(
            p.border_title == "пути" for p in scr.query(".config-panel")
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"agent.auto_routing": False},
        {"backend.api_mode": "responses"},
        {"models": {"only": {}}, "slots": {"default": "only"}},
    ],
    ids=["auto_routing_off", "responses", "one_model"],
)
def test_profile_header_shows_the_working_level_when_auto_is_unavailable(
    extra,
):
    from gc_tui.screens.config import _profile_header_text

    cfg = {
        "agent.model": "m",
        "level": "auto",
        "backend.api_mode": "chat_completions",
        "agent.auto_routing": True,
        "models": {"fast": {}, "mid": {}, "big": {}},
        "slots": {"light": "fast", "default": "mid", "max": "big"},
        **extra,
    }
    text = _profile_header_text(as_settings(cfg), profile_source=None).plain
    assert "уровень: default" in text
