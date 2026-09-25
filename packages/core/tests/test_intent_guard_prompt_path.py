# tests/test_intent_guard_prompt_path.py
"""Finish-gate prompt loading honours a config-provided path with a source-checkout
fallback — mirrors system/summary prompt loading so non-editable installs (Harbor
image) can point at the bundled prompt instead of fail-opening the gate."""

from pathlib import Path

import pytest
from gc_core.intent_guard.final_report_consistency import (
    DEFAULT_INTENT_GUARD_PROMPT_PATH,
    _load_guard_prompt,
)


def test_uses_config_provided_path(tmp_path):
    p = tmp_path / "guard.md"
    p.write_text("CUSTOM GUARD PROMPT", encoding="utf-8")
    assert _load_guard_prompt(str(p)) == "CUSTOM GUARD PROMPT"


def test_falls_back_to_default_when_path_none():
    # DEFAULT exists in the source checkout; returns the real (non-empty) prompt.
    assert _load_guard_prompt(None).strip() != ""
    assert _load_guard_prompt().strip() != ""


def test_missing_path_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        _load_guard_prompt(str(tmp_path / "does_not_exist.md"))


def test_empty_prompt_raises_valueerror(tmp_path):
    p = tmp_path / "empty.md"
    p.write_text("   \n", encoding="utf-8")
    with pytest.raises(ValueError):
        _load_guard_prompt(str(p))


def test_default_path_points_at_repo_prompts():
    assert (
        Path(DEFAULT_INTENT_GUARD_PROMPT_PATH)
        .as_posix()
        .endswith("prompts/chat_completions_intent_guard_prompt.md")
    )
