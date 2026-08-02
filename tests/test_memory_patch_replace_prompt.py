"""Контракт prompts/chat_completions_memory_patch_replace.md."""

from pathlib import Path
from types import SimpleNamespace

from summarize.memory_suggestions import request_patch_replace
from summarize.prompt_loading import (
    MEMORY_PATCH_SUBAGENT_GUIDANCE_PLACEHOLDER,
    MEMORY_PATCH_SUBAGENT_GUIDANCE_TEXT,
    load_memory_patch_replace_prompt,
)
from summarize.summary_spec import (
    MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER,
    SUMMARY_SECTIONS,
)
from summarize.working_memory_gateway import MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER, REGISTER_ORDER


def _prompt_path() -> str:
    root = Path(__file__).resolve().parent.parent
    return str(root / "prompts" / "chat_completions_memory_patch_replace.md")


def test_patch_replace_template_has_placeholders_on_disk():
    raw = Path(_prompt_path()).read_text(encoding="utf-8")
    assert MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER in raw
    assert MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER in raw
    assert MEMORY_PATCH_SUBAGENT_GUIDANCE_PLACEHOLDER in raw


def test_patch_replace_prompt_injects_summary_tags_and_wm_registers():
    loaded = load_memory_patch_replace_prompt(_prompt_path())
    assert MEMORY_PATCH_SUMMARY_TAGS_PLACEHOLDER not in loaded
    assert MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER not in loaded
    assert MEMORY_PATCH_SUBAGENT_GUIDANCE_PLACEHOLDER not in loaded
    assert "Subagent run" not in loaded
    for section in SUMMARY_SECTIONS:
        assert f"`{section.tag}`" in loaded
        assert section.memory_patch_hint in loaded
    for name in REGISTER_ORDER:
        assert f"<{name}>..." in loaded
    assert "user_intent" not in loaded.lower()
    assert "never basename-only" in loaded
    assert "absolute" in loaded.lower()


def test_patch_replace_prompt_injects_subagent_guidance_when_flagged():
    loaded = load_memory_patch_replace_prompt(_prompt_path(), subagent=True)
    assert MEMORY_PATCH_SUBAGENT_GUIDANCE_PLACEHOLDER not in loaded
    assert "Subagent run (shared thread memory)" in loaded
    assert "Do **not** rewrite" in loaded
    assert "Prefer durable updates in `<knowledge>`" in loaded
    assert "clobber orchestrator goal/state" in loaded
    # Core addendum body is present (not just the heading).
    assert "shared with the orchestrator" in loaded
    assert MEMORY_PATCH_SUBAGENT_GUIDANCE_TEXT.splitlines()[0] in loaded


def test_request_patch_replace_injects_subagent_guidance_in_system_only(monkeypatch):
    captured: dict = {}

    def _fake_summary_text(*, ctx, state, messages, request_label, logger=None, **kwargs):
        captured["messages"] = messages
        captured["label"] = request_label
        return "<goal>keep</goal>"

    monkeypatch.setattr(
        "summarize.memory_suggestions.request_summary_text",
        _fake_summary_text,
    )
    ctx = SimpleNamespace(config_dict={}, logger=None)
    child = SimpleNamespace(parent_state_id="parent-1")
    request_patch_replace(
        ctx=ctx,
        state=child,
        history_text="hist",
        summary_text="<summary/>",
        persistent_state_block="<persistent_state/>",
    )
    system = captured["messages"][0]["content"]
    user = captured["messages"][1]["content"]
    assert "Subagent run (shared thread memory)" in system
    assert "patch_context" not in user
    assert user.startswith("<dialog_history>")

    main = SimpleNamespace(parent_state_id=None)
    request_patch_replace(
        ctx=ctx,
        state=main,
        history_text="hist",
        summary_text="<summary/>",
        persistent_state_block="<persistent_state/>",
    )
    system_main = captured["messages"][0]["content"]
    assert "Subagent run (shared thread memory)" not in system_main
