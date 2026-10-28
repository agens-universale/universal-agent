"""Плейсхолдеры WM-регистров в промптах суммаризации и memory patch."""

from pathlib import Path

from gc_core.summarize.prompt_loading import (
    render_memory_patch_prompt,
    render_summary_prompt,
)
from gc_core.summarize.working_memory_gateway import (
    MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER,
    REGISTER_ORDER,
    REGISTER_PATCH_RULES,
    SESSION_MEMORY_REGISTER_NAMES_PLACEHOLDER,
)
from gc_testkit.paths import ASSETS


def default_prompt_path() -> str:
    return str(ASSETS / "prompts" / "chat_completions_summary_prompt.md")


def load_prompt_template(path: str) -> str:
    raw = Path(path).read_text(encoding="utf-8")
    return render_summary_prompt(raw, label=path)


def load_memory_patch_replace_prompt(path: str) -> str:
    raw = Path(path).read_text(encoding="utf-8")
    return render_memory_patch_prompt(raw, label=path)


def test_summary_prompt_injects_register_names():
    raw = Path(default_prompt_path()).read_text(encoding="utf-8")
    assert SESSION_MEMORY_REGISTER_NAMES_PLACEHOLDER in raw
    assert "never basename-only" in raw
    loaded = load_prompt_template(default_prompt_path())
    assert SESSION_MEMORY_REGISTER_NAMES_PLACEHOLDER not in loaded
    for name in REGISTER_ORDER:
        assert name in loaded
    assert "never basename-only" in loaded
    assert "absolute" in loaded.lower()


def test_replace_prompt_still_uses_all_registers_from_canon():
    path = str(ASSETS / "prompts" / "chat_completions_memory_patch_replace.md")
    loaded = load_memory_patch_replace_prompt(path)
    assert MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER not in loaded
    for name in REGISTER_ORDER:
        assert f"**{name}:**" in loaded
        assert REGISTER_PATCH_RULES[name] in loaded
