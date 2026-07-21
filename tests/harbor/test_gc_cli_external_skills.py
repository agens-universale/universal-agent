"""Harbor-injected skills surfacing in gc_cli_external.

Mirrors terminus_2: harbor drops task skills into the container at ``skills_dir`` and the
adapter must advertise them to the agent. These tests exercise the discovery/formatting
logic against a fake environment (no real container needed).
"""
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# Expose `adapters/` (which lives inside ./harbor) and `src/` for the adapter import.
# The installed `harbor` pip lib is a regular package, so it wins over the repo's
# ./harbor namespace dir regardless of sys.path order. In the real bench
# PYTHONPATH=/opt/gc-harbor-adapters, so `adapters` is top-level anyway.
for p in (str(_REPO / "harbor"), str(_REPO / "src")):
    if p not in sys.path:
        sys.path.append(p)

from adapters.gc_cli_external import GcCliExternal  # noqa: E402


class _ExecResult:
    def __init__(self, return_code=0, stdout=""):
        self.return_code = return_code
        self.stdout = stdout


class _FakeEnv:
    """Minimal BaseEnvironment stand-in: a virtual FS of {path: content}."""

    def __init__(self, skills_dir, files):
        self._skills_dir = skills_dir
        self._files = files  # {abs_path: content}

    async def is_dir(self, path, user=None):
        return path == self._skills_dir

    async def exec(self, command, timeout_sec=None, **kw):
        if command.startswith("find "):
            found = "\n".join(
                sorted(p for p in self._files if p.endswith("/SKILL.md"))
            )
            return _ExecResult(0, found)
        if command.startswith("cat "):
            path = command.split(" ", 1)[1].strip().strip("'")
            if path in self._files:
                return _ExecResult(0, self._files[path])
            return _ExecResult(1, "")
        return _ExecResult(1, "")


def _agent(skills_dir):
    a = object.__new__(GcCliExternal)  # skip BaseAgent.__init__
    a.skills_dir = skills_dir
    return a


SKILL_MD = (
    "---\n"
    "name: mesh-analysis\n"
    "description: Analyze STL meshes and compute mass by material.\n"
    "---\n\n"
    "# Mesh analysis\nRun scripts/mesh_tool.py ...\n"
)


def test_frontmatter_parses_name_and_description():
    fm = GcCliExternal._parse_skill_frontmatter(SKILL_MD)
    assert fm == {
        "name": "mesh-analysis",
        "description": "Analyze STL meshes and compute mass by material.",
    }


def test_frontmatter_rejects_missing_fields():
    assert GcCliExternal._parse_skill_frontmatter("no frontmatter here") is None
    assert GcCliExternal._parse_skill_frontmatter("---\nname: x\n---") is None


@pytest.mark.asyncio
async def test_build_skills_section_advertises_injected_skill():
    env = _FakeEnv(
        "/skills", {"/skills/mesh-analysis/SKILL.md": SKILL_MD}
    )
    section = await _agent("/skills")._build_skills_section(env)
    assert section is not None
    assert "<available_skills>" in section
    assert "mesh-analysis" in section
    assert "Analyze STL meshes" in section
    # the model needs the absolute in-container path to read the skill on demand
    assert "/skills/mesh-analysis/SKILL.md" in section


@pytest.mark.asyncio
async def test_no_skills_dir_returns_none():
    assert await _agent(None)._build_skills_section(_FakeEnv("/skills", {})) is None


@pytest.mark.asyncio
async def test_empty_skills_dir_returns_none():
    # dir exists but holds no SKILL.md -> nothing to advertise
    section = await _agent("/skills")._build_skills_section(_FakeEnv("/skills", {}))
    assert section is None


if __name__ == "__main__":
    # Runnable without pytest: `python tests/harbor/test_gc_cli_external_skills.py`.
    # (Local pytest collection can trip over the ./harbor namespace vs installed-harbor
    # sys.path ordering; direct execution and the bench PYTHONPATH do not.)
    import asyncio

    test_frontmatter_parses_name_and_description()
    test_frontmatter_rejects_missing_fields()
    asyncio.run(test_build_skills_section_advertises_injected_skill())
    asyncio.run(test_no_skills_dir_returns_none())
    asyncio.run(test_empty_skills_dir_returns_none())
    print("ok")
