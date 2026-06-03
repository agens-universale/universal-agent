"""Guard: the bundled supercoder pack stays tool-agnostic.

Skills must NOT hardcode runtime tool names (read_file, memory_files,
execute_command, ...). Only the skill-discovery tools skills_load/skills_list
may be named explicitly. Also pins the durability semantic the memory_files
references encoded (executing-plans progress / systematic-debugging repro
must still state they survive context summarization).
"""

import re
from pathlib import Path

import pytest

PACK_DIR = Path(__file__).resolve().parents[1] / "skills" / "supercoder"

FORBIDDEN_RUNTIME_TOOLS = (
    "read_file",
    "list_files",
    "write_to_file",
    "append_to_file",
    "execute_command",
    "memory_files",
    "turn_finish",
)

_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN_RUNTIME_TOOLS) + r")\b")


def _md_files():
    return sorted(PACK_DIR.rglob("*.md"))


def test_pack_has_markdown():
    assert _md_files(), f"no markdown found under {PACK_DIR}"


@pytest.mark.parametrize(
    "md", _md_files(), ids=lambda p: str(p.relative_to(PACK_DIR))
)
def test_no_runtime_tool_names(md):
    offenders = []
    for i, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        m = _FORBIDDEN_RE.search(line)
        if m:
            offenders.append(
                f"{md.relative_to(PACK_DIR)}:{i}: {m.group(1)} :: {line.strip()}"
            )
    assert not offenders, "hardcoded runtime tool name(s):\n" + "\n".join(offenders)


def test_skill_discovery_tools_not_flagged():
    assert not _FORBIDDEN_RE.search('skills_load("supercoder:brainstorming")')
    assert not _FORBIDDEN_RE.search("skills_list(pack='supercoder')")


@pytest.mark.parametrize(
    "rel", ["executing-plans/SKILL.md", "systematic-debugging/SKILL.md"]
)
def test_durability_semantic_preserved(rel):
    body = (PACK_DIR / rel).read_text(encoding="utf-8").lower()
    assert "summariz" in body, (
        f"{rel} lost the 'survives summarization' durability semantic "
        "that the memory_files reference encoded"
    )
