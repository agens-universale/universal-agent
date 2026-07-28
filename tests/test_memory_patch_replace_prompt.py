"""Контракт prompts/chat_completions_memory_patch_replace.md."""

from pathlib import Path

from summarize.prompt_loading import load_memory_patch_replace_prompt
from summarize.summary_spec import (
    MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER,
    SUMMARY_SECTIONS,
    section_tags,
)
from summarize.working_memory_gateway import MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER, REGISTER_ORDER


def _prompt_path() -> str:
    root = Path(__file__).resolve().parent.parent
    return str(root / "prompts" / "chat_completions_memory_patch_replace.md")


def test_patch_replace_template_has_placeholders_on_disk():
    raw = Path(_prompt_path()).read_text(encoding="utf-8")
    assert MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER in raw
    assert MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER in raw


def test_patch_replace_prompt_injects_summary_tags_and_wm_registers():
    loaded = load_memory_patch_replace_prompt(_prompt_path())
    assert MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER not in loaded
    assert MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER not in loaded
    for section in SUMMARY_SECTIONS:
        assert f"`{section.tag}`" in loaded
        assert section.memory_patch_hint in loaded
    for name in REGISTER_ORDER:
        assert f"<{name}>..." in loaded
    assert "user_intent" not in loaded.lower()
    assert "never basename-only" in loaded
    assert "absolute" in loaded.lower()
