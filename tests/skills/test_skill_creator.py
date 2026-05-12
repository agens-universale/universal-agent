"""Smoke-tests for skills/skill-creator/."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = PROJECT_ROOT / "skills" / "skill-creator"


def _registry():
    """Build a fresh SkillsRegistry pointing at the project's skills/.

    Defensive cleanup: tests/test_summary_triggers.py stubs `tools.skills.registry`
    in sys.modules at import time. Pop any stale entries before our import so
    the test works both standalone and in the full suite.
    """
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    for name in list(sys.modules):
        if name == "tools" or name.startswith("tools."):
            sys.modules.pop(name, None)
    from tools.skills.registry import SkillsRegistry  # noqa: E402

    return SkillsRegistry(skills_root=str(PROJECT_ROOT / "skills"))


def test_skill_creator_loads_via_registry():
    """skill-creator must be discoverable in the registry with correct metadata.

    In the lean version we don't ship references/ — so detail['references']
    should be empty.
    """
    registry = _registry()
    detail = registry.load_skill_detail("skill-creator")

    assert detail["id"] == "skill-creator"
    assert detail["name"]
    assert "skill" in detail["description"].lower()
    assert detail["instructions"], "SKILL.md body must not be empty"
    assert detail["has_scripts"] is True
    assert detail["references"] == [], detail["references"]


def test_quick_validate_runs_on_existing_skill():
    """quick_validate.py must validate an existing project skill (after the Task 3 adaptations)."""
    target = PROJECT_ROOT / "skills" / "article-creator"
    assert target.is_dir()

    result = subprocess.run(
        [sys.executable, "-m", "scripts.quick_validate", str(target.resolve())],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=30,
        cwd=str(SKILL_DIR),
    )
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "valid" in result.stdout.lower()


def test_package_skill_produces_zip(tmp_path):
    """package_skill.py must build a valid .skill (zip) containing SKILL.md."""
    target = PROJECT_ROOT / "skills" / "article-creator"
    assert target.is_dir()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.package_skill",
            str(target.resolve()),
            str(tmp_path),
        ],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=60,
        cwd=str(SKILL_DIR),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    candidates = list(tmp_path.glob("*.skill"))
    assert candidates, f"no .skill produced; stdout={result.stdout}"

    import zipfile

    with zipfile.ZipFile(candidates[0]) as zf:
        names = zf.namelist()
        assert any(n.endswith("SKILL.md") for n in names), names
