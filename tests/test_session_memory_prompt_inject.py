"""Плейсхолдеры WM-регистров в промптах суммаризации и memory patch."""

from pathlib import Path

from summarize.prompt_loading import (
    default_prompt_path,
    load_memory_patch_replace_prompt,
    load_prompt_template,
)
from summarize.working_memory_gateway import (
    MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER,
    SESSION_MEMORY_REGISTER_NAMES_PLACEHOLDER,
    REGISTER_ORDER,
    REGISTER_PATCH_RULES,
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


def test_replace_prompt_still_uses_all_registers_from_canon():
    path = str(_root() / "prompts" / "chat_completions_memory_patch_replace.md")
    loaded = load_memory_patch_replace_prompt(path)
    assert MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER not in loaded
    for name in REGISTER_ORDER:
        assert f"**{name}:**" in loaded
        assert REGISTER_PATCH_RULES[name] in loaded
