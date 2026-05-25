"""Guard persistent-memory guidance in demo_cli_agent.md (deployment-agnostic)."""
from pathlib import Path


_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "demo_cli_agent.md"
)


def _read_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_demo_cli_agent_md_has_persistent_memory_section():
    text = _read_prompt()
    assert "## Persistent memory (when tools provide it)" in text


def test_protocol_is_deployment_agnostic():
    section = _read_prompt()
    flat = _flat(section)
    assert "MCP" in flat or "built-in" in flat
    assert "do not assume fixed tool names" in flat or "tools listed in" in flat
    assert "FIRST tool call" not in flat
    assert "non-negotiable" not in flat


def test_no_conditional_prompt_tags():
    text = _read_prompt()
    assert "<memory_files_prompt>" not in text
    assert "<long_memory_prompt>" not in text
