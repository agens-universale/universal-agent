"""Guard the system-prompt-level Long memory protocol section in demo_cli_agent.md.

Third pressure point of the imperative read protocol (alongside Project Info
nudge and tool description workflow).
"""
from pathlib import Path


_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "prompts" / "demo_cli_agent.md"
)


def _read_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Collapse whitespace so substring asserts ignore markdown line-wrapping."""
    return " ".join(text.split())


def test_demo_cli_agent_md_has_long_memory_protocol_section():
    text = _read_prompt()
    assert "## Long memory protocol" in text


def test_protocol_section_contains_mandatory_first_step_rule():
    text = _flat(_read_prompt())
    assert "MUST" in text
    assert "FIRST tool call in this session" in text


def test_protocol_section_contains_override_rule():
    text = _flat(_read_prompt())
    assert "even if they contradict what existing code shows" in text


def test_protocol_section_does_not_leak_path_or_tool_name():
    """The protocol must guide behavior without disclosing internals."""
    text = _read_prompt()
    # The protocol section is one block; verify the section itself is clean
    # rather than the whole agent.md (other sections may mention unrelated paths).
    start = text.index("## Long memory protocol")
    # Section ends at the next "## " header
    rest = text[start + len("## Long memory protocol") :]
    end_offset = rest.find("\n## ")
    section = rest if end_offset == -1 else rest[:end_offset]
    assert ".gc/" not in section
    assert "long_memory.md" not in section
    assert "long_memory" not in section  # tool name must not appear
