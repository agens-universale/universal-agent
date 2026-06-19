"""A: Output formatting nudge — секция в системных промптах chat_completions."""
import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_prompt(name: str) -> str:
    return (_REPO_ROOT / "prompts" / name).read_text(encoding="utf-8")


def _section(body: str, header: str) -> str:
    """Текст секции от заголовка до следующего markdown-заголовка (## или ###)."""
    start = body.index(header)
    rest = body[start + len(header):]
    m = re.search(r"\n#{2,3} ", rest)
    return rest[: m.start()] if m else rest


def test_tui_file_has_output_formatting_section():
    body = _read_prompt("chat_completions_tui_special_instructions.md")
    assert "### Output formatting" in body
    assert "fenced markdown" in body
    assert "language tag" in body
    assert "single backticks" in body


def test_base_prompt_has_no_output_formatting():
    body = _read_prompt("chat_completions_system_prompt.md")
    assert "### Output formatting" not in body
    assert "fenced markdown" not in body


def test_subagent_prompt_has_no_branch_output_formatting():
    body = _read_prompt("chat_completions_subagent_system_prompt.md")
    assert "### Output formatting" not in body


def test_tui_file_output_formatting_section_is_tool_agnostic():
    body = _read_prompt("chat_completions_tui_special_instructions.md")
    section = _section(body, "### Output formatting")
    assert "submit_solution" not in section


def test_tui_file_steers_questions_to_asking_tool():
    body = _read_prompt("chat_completions_tui_special_instructions.md")
    assert "ask tool" in body
    assert "wait for human input" in body


def test_tui_file_asking_vs_finalizing_rule():
    body = _read_prompt("chat_completions_tui_special_instructions.md")
    assert "ask tool" in body
    assert "submit_solution" not in body
    assert "turn-finish tool" in body
    assert "forward-looking, non-blocking" in body


def test_base_prompt_has_no_asking_block():
    body = _read_prompt("chat_completions_system_prompt.md")
    assert "designated channel" not in body
    assert "invisible to the user" not in body
