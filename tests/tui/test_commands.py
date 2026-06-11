"""Реестр команд: метаданные, lookup, closest-match (§4)."""
from tui.commands import (
    Command, CommandRegistry, build_default_registry, closest_command,
)
from tui import branding


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
              "status", "export", "help", "quit"]:
        assert reg.lookup(n) is not None, f"missing /{n}"


def test_default_registry_is_exactly_keep_set():
    reg = build_default_registry()
    names = sorted(c.name for c in reg.list_commands())
    assert names == sorted([
        "sessions", "new", "stop", "skill", "skills",
        "status", "config", "mcp", "export", "help", "quit",
    ])


def test_skill_descriptions_use_navyk_not_skill_word():
    reg = build_default_registry()
    assert reg.lookup("skill").description == "использовать навык"
    assert reg.lookup("skills").description == "обзор навыков"


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
    assert names == ["stop", "tasks"]  # alphabetical, no aliases


def _cmd(name):
    return build_default_registry().lookup(name)


def test_status_has_no_raw_arg():
    assert _cmd("status").args == ""


def test_command_hotkeys():
    expected = {
        "help": "F1", "new": "Ctrl+N", "sessions": "Ctrl+S",
        "export": "", "stop": "Ctrl+C", "quit": "Esc",
        "skill": "", "skills": "", "status": "",
    }
    reg = build_default_registry()
    for name, key in expected.items():
        assert reg.lookup(name).hotkey == key, name


def test_hotkeys_match_app_bindings():
    """Каждая команда с hotkey имеет совпадающую привязку в app.BINDINGS."""
    from tui.app import TuiApp
    binding_keys = {b.key for b in TuiApp.BINDINGS}

    def normalize(display: str) -> str:
        k = display.lower()
        return "escape" if k == "esc" else k

    reg = build_default_registry()
    for cmd in reg.list_commands():
        if cmd.hotkey:
            assert normalize(cmd.hotkey) in binding_keys, f"{cmd.name}: {cmd.hotkey}"


def test_headers_have_no_fake_version():
    from tui.screens.main import _HEADER_RIGHT as MAIN_RIGHT
    from tui.screens.skills import _HEADER_RIGHT as SK_RIGHT
    for s in (MAIN_RIGHT, SK_RIGHT):
        assert "v0.2" not in s
        assert branding.HEADER_RIGHT in s
