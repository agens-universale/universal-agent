"""Guard the long_memory tool description shape.

After iter-3 the description is intentionally minimal (~250 chars):
no workflow, no negation, no mention of other tools. Workflow lives in
the system prompt (`prompts/<agent_md>`::## Long memory protocol).
"""
from tools.session.long_memory import LongMemory


def test_tool_description_is_short():
    desc = LongMemory.tool_description()["description"]
    assert len(desc) < 400, (
        f"long_memory description is {len(desc)} chars; weak models "
        f"degrade past ~400. Keep it lean — workflow belongs in system prompt."
    )


def test_tool_description_states_purpose_and_actions():
    desc = LongMemory.tool_description()["description"]
    assert "Project/long memory" in desc
    assert "FUTURE sessions" in desc
    assert "Survives restarts" in desc
    # Three actions must be named in description (so model knows action enum
    # carries meaning) but without per-action workflow prose.
    assert "read" in desc
    assert "append" in desc
    assert "write" in desc


def test_tool_description_has_no_negation_or_other_tool_mention():
    """Negation and cross-tool names hurt dispatch on weak models."""
    desc = LongMemory.tool_description()["description"]
    assert "memory_files" not in desc
    assert "Do NOT" not in desc
    assert "MANDATORY" not in desc  # workflow moved to system prompt
    assert "FIRST STEP" not in desc


def test_tool_description_does_not_leak_path_or_filename():
    desc = LongMemory.tool_description()["description"]
    assert ".gc/" not in desc
    assert "long_memory.md" not in desc
