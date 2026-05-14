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


def test_package_skill_produces_zip_with_correct_structure(tmp_path):
    """package_skill.py must produce skill-<id>.zip with <id>/SKILL.md inside."""
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

    candidates = list(tmp_path.glob("skill-*.zip"))
    assert candidates, f"no skill-*.zip produced; stdout={result.stdout}"

    skill_zip = candidates[0]
    assert skill_zip.name == "skill-article-creator.zip", skill_zip.name

    import zipfile
    with zipfile.ZipFile(skill_zip) as zf:
        names = zf.namelist()
        assert "article-creator/SKILL.md" in names, names


def test_package_skill_includes_readme_with_metadata(tmp_path):
    """Archive must contain README.md at root with id, version, description from frontmatter."""
    target = PROJECT_ROOT / "skills" / "article-creator"

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

    skill_zip = next(tmp_path.glob("skill-*.zip"))

    import zipfile
    with zipfile.ZipFile(skill_zip) as zf:
        assert "README.md" in zf.namelist()
        readme_content = zf.read("README.md").decode("utf-8")
        assert "article-creator" in readme_content
        assert "Как установить" in readme_content
        assert "demo/" in readme_content or "Что в архиве" in readme_content


def test_package_skill_explicit_source_layer(tmp_path):
    """--source-layer flag should propagate into README."""
    target = PROJECT_ROOT / "skills" / "article-creator"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.package_skill",
            str(target.resolve()),
            "--source-layer", "user",
            "--output-dir", str(tmp_path),
        ],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=60,
        cwd=str(SKILL_DIR),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    skill_zip = next(tmp_path.glob("skill-*.zip"))
    import zipfile
    with zipfile.ZipFile(skill_zip) as zf:
        readme = zf.read("README.md").decode("utf-8")
        assert "**Слой-источник:** user" in readme, readme[:500]


@pytest.mark.parametrize("skill_subpath, expected_layer", [
    # Project layer: under <cwd>/.gc/skills/
    (".gc/skills/test-skill", "project"),
    # System layer: under <repo>/skills/
    ("skills/test-skill", "system"),
])
def test_package_skill_auto_detects_source_layer(tmp_path, skill_subpath, expected_layer):
    """Without --source-layer, README should auto-detect from path."""
    # Build a minimal valid skill at the given location relative to PROJECT_ROOT
    skill_dir = PROJECT_ROOT / skill_subpath
    cleanup_needed = not skill_dir.exists()
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nid: test-skill\nname: test-skill\ndescription: Test\n---\n# Test\n",
        encoding="utf-8",
    )

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.package_skill",
                str(skill_dir.resolve()),
                "--output-dir", str(tmp_path),
            ],
            capture_output=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=60,
            cwd=str(SKILL_DIR),
        )
        assert result.returncode == 0, (result.stdout, result.stderr)

        skill_zip = next(tmp_path.glob("skill-*.zip"))
        import zipfile
        with zipfile.ZipFile(skill_zip) as zf:
            readme = zf.read("README.md").decode("utf-8")
            assert f"**Слой-источник:** {expected_layer}" in readme, readme[:500]
    finally:
        if cleanup_needed:
            import shutil
            shutil.rmtree(skill_dir, ignore_errors=True)


def test_package_skill_unknown_source_layer(tmp_path):
    """Skill in random tmp location → source-layer 'unknown'."""
    skill_dir = tmp_path / "random-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nid: random-skill\nname: random-skill\ndescription: Test\n---\n# Test\n",
        encoding="utf-8",
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.package_skill",
            str(skill_dir.resolve()),
            "--output-dir", str(out_dir),
        ],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=60,
        cwd=str(SKILL_DIR),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    skill_zip = next(out_dir.glob("skill-*.zip"))
    import zipfile
    with zipfile.ZipFile(skill_zip) as zf:
        readme = zf.read("README.md").decode("utf-8")
        assert "**Слой-источник:** unknown" in readme


def test_package_skill_with_dev_folder_puts_into_demo(tmp_path):
    """If <skill-folder>/_dev/ exists, its contents go to demo/ in archive."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nid: test-skill\nname: test-skill\ndescription: Test\n---\n# Test\n",
        encoding="utf-8",
    )

    # Add _dev/ with sample artifacts
    dev_dir = skill_dir / "_dev"
    dev_dir.mkdir()
    (dev_dir / "intent_brief.md").write_text("# intent\n", encoding="utf-8")
    examples_dir = dev_dir / "examples"
    examples_dir.mkdir()
    (examples_dir / "prompt_01.md").write_text("# prompt 1\n", encoding="utf-8")
    (examples_dir / "prompt_01_output.md").write_text("# output 1\n", encoding="utf-8")

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.package_skill",
            str(skill_dir.resolve()),
            "--output-dir", str(out_dir),
        ],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=60,
        cwd=str(SKILL_DIR),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    skill_zip = next(out_dir.glob("skill-*.zip"))
    import zipfile
    with zipfile.ZipFile(skill_zip) as zf:
        names = zf.namelist()
        # demo/ contents from _dev/
        assert "demo/intent_brief.md" in names, names
        assert "demo/examples/prompt_01.md" in names, names
        assert "demo/examples/prompt_01_output.md" in names, names
        # _dev/ NOT inside <id>/ part of archive
        assert "test-skill/_dev/intent_brief.md" not in names, names


def test_package_skill_cleans_up_dev_folder_after_success(tmp_path):
    """After successful packaging, <skill-folder>/_dev/ must be removed."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nid: test-skill\nname: test-skill\ndescription: Test\n---\n# Test\n",
        encoding="utf-8",
    )
    dev_dir = skill_dir / "_dev"
    dev_dir.mkdir()
    (dev_dir / "intent_brief.md").write_text("# intent\n", encoding="utf-8")

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.package_skill",
            str(skill_dir.resolve()),
            "--output-dir", str(out_dir),
        ],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=60,
        cwd=str(SKILL_DIR),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    # _dev/ должен быть удалён
    assert not dev_dir.exists(), f"_dev/ still exists at {dev_dir}"
    # При этом сам скилл остался
    assert (skill_dir / "SKILL.md").exists()


def test_package_skill_cleanup_permission_error_does_not_block(tmp_path, monkeypatch):
    """If _dev/ cleanup raises OSError, archive should still be created and exit 0."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nid: test-skill\nname: test-skill\ndescription: Test\n---\n# Test\n",
        encoding="utf-8",
    )
    dev_dir = skill_dir / "_dev"
    dev_dir.mkdir()
    (dev_dir / "intent_brief.md").write_text("# intent\n", encoding="utf-8")

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    # Direct import approach (subprocess can't be easily monkeypatched):
    sys.path.insert(0, str(PROJECT_ROOT / "skills" / "skill-creator" / "scripts"))
    import importlib
    if "package_skill" in sys.modules:
        importlib.reload(sys.modules["package_skill"])
    import package_skill as ps

    import shutil as _shutil
    original_rmtree = _shutil.rmtree

    def fake_rmtree(path, *args, **kwargs):
        raise PermissionError(f"mock: cannot remove {path}")

    monkeypatch.setattr(_shutil, "rmtree", fake_rmtree)
    try:
        result = ps.package_skill(str(skill_dir), output_dir=str(out_dir))
    finally:
        monkeypatch.setattr(_shutil, "rmtree", original_rmtree)

    # Archive created despite cleanup failure
    assert result is not None, "archive should be created"
    skill_zip = next(out_dir.glob("skill-*.zip"))
    assert skill_zip.exists()
    # _dev/ остался (cleanup упал)
    assert dev_dir.exists()


def test_package_skill_without_dev_folder(tmp_path):
    """Skill без _dev/ → архив без demo/, README без секции demo."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nid: test-skill\nname: test-skill\ndescription: Test\n---\n# Test\n",
        encoding="utf-8",
    )

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.package_skill",
            str(skill_dir.resolve()),
            "--output-dir", str(out_dir),
        ],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=60,
        cwd=str(SKILL_DIR),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)

    skill_zip = next(out_dir.glob("skill-*.zip"))
    import zipfile
    with zipfile.ZipFile(skill_zip) as zf:
        names = zf.namelist()
        # No demo/ at all
        assert not any(n.startswith("demo/") for n in names), names
        # README без секции demo
        readme = zf.read("README.md").decode("utf-8")
        assert "demo/" not in readme or "Что в архиве" not in readme.split("demo/")[0]
        # (allow mention only in absence)
