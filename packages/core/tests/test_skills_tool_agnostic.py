"""Guard: all bundled skills stay tool-agnostic (TOOL_NAMESPACES_SPEC §7.4).

Skills describe *roles* (filesystem write, human clarification, finish handoff,
delegated subagent, MCP literature search on science-mcp, …). They must NOT
hardcode runtime builtin tool names (as-is or to-be), except the skills
mechanism itself: ``skills_list``, ``skills_load``, ``skills_run_script``,
``skills_read_reference``, ``skills_read_asset``.
"""

import re
from pathlib import Path

import pytest
from gc_testkit.paths import ASSETS

SKILLS_DIR = ASSETS / "skills"
SUPERCODER_DIR = SKILLS_DIR / "supercoder"

# Legacy as-is and to-be agent-visible builtin names (not skills_*).
FORBIDDEN_RUNTIME_TOOLS = (
    "read_file",
    "list_files",
    "write_to_file",
    "append_to_file",
    "execute_command",
    "run_python_code",
    "ask_human",
    "submit_solution",
    "intermediate_reply",
    "memory_files",
    "long_memory",
    "turn_finish",
    "turn_ask_human",
    "turn_intermediate_reply",
    "fs_read_file",
    "fs_write_file",
    "fs_list_files",
    "fs_append_file",
    "exec_command",
    "exec_python",
    "exec_fetch_url",
    "session_memory",
    "session_think",
    "project_memory",
    "subagent_worker",
    "subagent_special_agent",
)

_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN_RUNTIME_TOOLS) + r")\b")

# Prefixed MCP tool ids from science-mcp (use plain capability names in skills).
_FORBIDDEN_SCIENCE_MCP_RE = re.compile(
    r"\bscience-(?:literature_search|preprint_search|work_lookup|"
    r"open_access_search|citations_by_doi|ncbi_fetch|explain_sources)\b"
)


def _md_files():
    return sorted(SKILLS_DIR.rglob("*.md"))


def test_skills_tree_has_markdown():
    assert _md_files(), f"no markdown found under {SKILLS_DIR}"


@pytest.mark.parametrize("md", _md_files(), ids=lambda p: str(p.relative_to(SKILLS_DIR)))
def test_no_runtime_tool_names(md):
    offenders = []
    for i, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        m = _FORBIDDEN_RE.search(line)
        if m:
            offenders.append(
                f"{md.relative_to(SKILLS_DIR)}:{i}: {m.group(1)} :: {line.strip()}"
            )
            continue
        m = _FORBIDDEN_SCIENCE_MCP_RE.search(line)
        if m:
            offenders.append(
                f"{md.relative_to(SKILLS_DIR)}:{i}: {m.group(0)} :: {line.strip()}"
            )
    assert not offenders, "hardcoded runtime tool name(s):\n" + "\n".join(offenders)


def test_skill_discovery_tools_not_flagged():
    assert not _FORBIDDEN_RE.search('skills_load("supercoder:brainstorming")')
    assert not _FORBIDDEN_RE.search("skills_list(pack='supercoder')")
    assert not _FORBIDDEN_RE.search("skills_run_script(skill_id='skill-creator'")
    assert not _FORBIDDEN_SCIENCE_MCP_RE.search(
        'skills_load("science-article-creator:scientific-research")'
    )


@pytest.mark.parametrize(
    "rel", ["executing-plans/SKILL.md", "systematic-debugging/SKILL.md"]
)
def test_supercoder_durability_semantic_preserved(rel):
    body = (SUPERCODER_DIR / rel).read_text(encoding="utf-8").lower()
    assert "summariz" in body, (
        f"{rel} lost the 'survives summarization' durability semantic "
        "that session memory references encoded"
    )
