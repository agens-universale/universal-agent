"""Плейсхолдеры WM-регистров в промптах суммаризации и memory patch."""

from pathlib import Path

from summarize.prompt_loading import (
    default_prompt_path,
    load_memory_patch_append_prompt,
    load_memory_patch_replace_prompt,
    load_prompt_template,
)
from summarize.working_memory_gateway import (
    MEMORY_PATCH_APPEND_OUTPUT_PLACEHOLDER,
    MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER,
    SESSION_MEMORY_REGISTER_NAMES_PLACEHOLDER,
    REGISTER_APPEND_ORDER,
    REGISTER_ORDER,
    REGISTER_PATCH_RULES,
    SESSION_MEMORY_REGISTERS,
)


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_summary_prompt_injects_register_names():
    raw = Path(default_prompt_path()).read_text(encoding="utf-8")
    assert SESSION_MEMORY_REGISTER_NAMES_PLACEHOLDER in raw
    loaded = load_prompt_template(default_prompt_path())
    assert SESSION_MEMORY_REGISTER_NAMES_PLACEHOLDER not in loaded
    for name in REGISTER_ORDER:
        assert name in loaded


def test_memory_patch_append_injects_append_registers_only():
    path = str(_root() / "prompts" / "chat_completions_memory_patch_append.md")
    raw = Path(path).read_text(encoding="utf-8")
    assert MEMORY_PATCH_APPEND_OUTPUT_PLACEHOLDER in raw
    loaded = load_memory_patch_append_prompt(path)
    assert MEMORY_PATCH_APPEND_OUTPUT_PLACEHOLDER not in loaded
    for name in REGISTER_APPEND_ORDER:
        assert f"<{name}>..." in loaded
        reg = next(r for r in SESSION_MEMORY_REGISTERS if r.name == name)
        assert reg.append_hint in loaded
    for name in REGISTER_ORDER:
        if name not in REGISTER_APPEND_ORDER:
            assert f"<{name}>..." not in loaded.split("Rules:")[0]
    for name, rule in REGISTER_PATCH_RULES.items():
        if name in REGISTER_APPEND_ORDER:
            assert rule not in loaded or name in loaded


def test_replace_prompt_still_uses_all_registers_from_canon():
    path = str(_root() / "prompts" / "chat_completions_memory_patch_replace.md")
    loaded = load_memory_patch_replace_prompt(path)
    assert MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER not in loaded
    for name in REGISTER_ORDER:
        assert f"**{name}:**" in loaded
        assert REGISTER_PATCH_RULES[name] in loaded
