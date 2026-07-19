"""Реестр команд: метаданные, lookup, closest-match (§4)."""
from tui.commands import (
    Command, CommandRegistry, build_default_registry, closest_command,
)


def test_register_and_lookup():
    reg = CommandRegistry()
    reg.register(Command(
        name="task", aliases=(), args="[-t id] [-s skill]",
        description="новая/подключение задачи",
    ))
    assert reg.lookup("task") is not None
    assert reg.lookup("nonexistent") is None


def test_alias_resolves_to_canonical():
    reg = CommandRegistry()
    reg.register(Command(name="tasks", aliases=("history",), args="[-r]",
                          description="список задач"))
    cmd = reg.lookup("history")
    assert cmd is not None and cmd.name == "tasks"


def test_closest_match_suggestion():
    reg = build_default_registry()
    suggestion = closest_command("statuz", reg)
    assert suggestion == "status"  # близкий канон


def test_closest_match_none_for_far_input():
    reg = build_default_registry()
    assert closest_command("zzzzzz12345", reg) is None


def test_default_registry_includes_core_commands():
    reg = build_default_registry()
    for n in ["sessions", "stop", "skills", "skill",
              "status", "export", "help", "quit", "view"]:
        assert reg.lookup(n) is not None, f"missing /{n}"


def test_requires_args_optional_vs_required():
    """`<…>` — обязательный аргумент; `[…]` и пусто — Enter выполняет сразу."""
    reg = build_default_registry()
    assert reg.lookup("view").requires_args is True
    assert reg.lookup("skill").requires_args is True
    assert reg.lookup("export").requires_args is False
    assert reg.lookup("help").requires_args is False


def test_default_registry_is_exactly_keep_set():
    reg = build_default_registry()
    names = sorted(c.name for c in reg.list_commands())
    assert names == sorted([
        "sessions", "new", "stop", "skill", "skills",
        "status", "config", "settings", "mcp", "notices", "diff", "view", "export", "help", "init", "compact", "quit",
        "gate", "answers",
    ])


def test_skill_descriptions_use_navyk_not_skill_word():
    reg = build_default_registry()
    assert reg.lookup("skill").description == "использовать навык"
    assert reg.lookup("skills").description == "обзор навыков"


def test_init_description_is_analysis_and_generation():
    reg = build_default_registry()
    assert reg.lookup("init").description == "анализ проекта и генерация AGENTS.md"


def test_sessions_command_has_no_aliases():
    reg = build_default_registry()
    assert reg.lookup("sessions") is not None
    for removed in ("resume", "tasks", "history"):
        assert reg.lookup(removed) is None


def test_sessions_description_has_no_picker_word():
    reg = build_default_registry()
    assert "picker" not in reg.lookup("sessions").description.lower()


def test_list_commands_returns_canonical_only():
    reg = CommandRegistry()
    reg.register(Command(name="tasks", aliases=("history",), args="", description=""))
    reg.register(Command(name="stop", aliases=(), args="", description=""))
    names = [c.name for c in reg.list_commands()]
    assert names == ["tasks", "stop"]  # порядок регистрации, без aliases


def _cmd(name):
    return build_default_registry().lookup(name)


def test_status_has_no_raw_arg():
    assert _cmd("status").args == ""


def test_command_hotkeys():
    expected = {
        "help": "F1 · Ctrl+/",
        "sessions": "F2 · Ctrl+S",
        "diff": "F3 · Ctrl+D",
        "notices": "F4 · Ctrl+O",
        "new": "Ctrl+N",
        "config": "F5 · Ctrl+G",
        "export": "", "compact": "", "init": "", "view": "",
        "skills": "", "skill": "", "status": "",
        "stop": "Ctrl+C", "mcp": "", "quit": "Esc",
        "gate": "", "answers": "",
    }
    reg = build_default_registry()
    for name, key in expected.items():
        assert reg.lookup(name).hotkey == key, name


def test_default_registry_frequency_order():
    names = [c.name for c in build_default_registry().list_commands()]
    assert names == [
        "help", "sessions", "diff", "view", "notices", "new", "config", "settings",
        "export", "compact", "init", "skills", "skill", "status",
        "stop", "mcp", "quit", "gate", "answers",
    ]


def test_hotkeys_match_app_bindings():
    """Каждый токен hotkey имеет совпадающую привязку в app.BINDINGS."""
    from tui.app import TuiApp
    binding_keys = {b.key for b in TuiApp.BINDINGS}

    def normalize(display: str) -> str:
        k = display.lower().replace(" ", "")
        aliases = {
            "esc": "escape",
            "ctrl+/": "ctrl+slash",
        }
        return aliases.get(k, k)

    reg = build_default_registry()
    for cmd in reg.list_commands():
        if not cmd.hotkey:
            continue
        for part in cmd.hotkey.split("·"):
            token = part.strip()
            if not token:
                continue
            assert normalize(token) in binding_keys, f"{cmd.name}: {token}"


def test_settings_command_description_mentions_server_address():
    reg = build_default_registry()
    assert reg.lookup("settings").description == "настройки: подключение, модель, MCP, обновление"


def test_compact_command_registered_with_alias():
    reg = build_default_registry()
    cmd = reg.lookup("compact")
    assert cmd is not None
    assert cmd.description == "сжать контекст сессии сейчас"
    # алиас /сжать ведёт на ту же команду
    assert reg.lookup("сжать") is cmd


def test_view_command_has_arg_file_flag():
    """/view — команда с файловым аргументом: открывает файловый пикер.
    Флаг в реестре, а не имя команды в обработчике (системность)."""
    reg = build_default_registry()
    assert reg.lookup("view").arg_file is True
    assert reg.lookup("help").arg_file is False   # дефолт не разлился


def test_arg_file_and_is_toggle_are_mutually_exclusive():
    """arg_file и is_toggle — взаимоисключающие режимы команды.

    Роутинг предполагает это молча: arg_file открывает файловый пикер в
    on_text_area_changed, is_toggle уводит Enter/←/→ в тоггл-ветку — команда
    с обоими флагами получила бы оба поведения сразу. Гвоздь фиксирует контракт.
    """
    reg = build_default_registry()
    for cmd in reg.list_commands():
        assert not (cmd.arg_file and cmd.is_toggle), cmd.name


def test_toggle_descriptions_generated_from_specs():
    from tui.commands import build_default_registry
    from tui.toggles import TOGGLES
    reg = build_default_registry()
    for name in ("gate", "answers"):
        cmd = reg.lookup(name)
        spec = TOGGLES[name]
        assert cmd.description == f"{spec.label}: {spec.summary}"


async def test_f6_opens_settings_with_input_focused():
    """F6 открывает /settings при фокусе в поле ввода.

    Регресс: у TextArea есть дефолтный биндинг f6=select_line — без priority на
    app-биндинге виджет перехватывал клавишу, и хоткей был мёртв (баг master)."""
    import pytest as _pytest
    _pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.settings import SettingsScreen

    app = TuiApp(config={})
    async with app.run_test() as pilot:
        app.screen.query_one("#cmdinput").focus()
        await pilot.pause()
        await pilot.press("f6")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
