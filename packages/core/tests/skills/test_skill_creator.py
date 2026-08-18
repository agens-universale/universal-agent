"""Smoke-tests for skills/skill-creator/."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from gc_testkit.paths import ASSETS

from gc_testkit.paths import PACKAGE_SRC_DIRS, REPO as PROJECT_ROOT
SKILL_DIR = ASSETS / "skills" / "skill-creator"


def _make_complete_dev_dir(skill_dir: Path) -> Path:
    """Создать `<skill-dir>/_dev/` с полным набором required artifacts.

    Используется тестами, которые тестируют другие аспекты упаковки
    (cleanup, demo-маппинг, и т.п.) и не должны падать на _dev/ gate.
    Required-набор:
      - intent_brief.md
      - completion_report.md
      - examples/prompt_01.md + prompt_01_output.md
      - examples/prompt_02.md + prompt_02_output.md
    """
    dev_dir = skill_dir / "_dev"
    dev_dir.mkdir(exist_ok=True)
    (dev_dir / "intent_brief.md").write_text("# intent\n", encoding="utf-8")
    (dev_dir / "completion_report.md").write_text("# completion\n", encoding="utf-8")
    examples_dir = dev_dir / "examples"
    examples_dir.mkdir(exist_ok=True)
    (examples_dir / "prompt_01.md").write_text("# prompt 1\n", encoding="utf-8")
    (examples_dir / "prompt_01_output.md").write_text("# output 1\n", encoding="utf-8")
    (examples_dir / "prompt_02.md").write_text("# prompt 2\n", encoding="utf-8")
    (examples_dir / "prompt_02_output.md").write_text("# output 2\n", encoding="utf-8")
    return dev_dir


@contextmanager
def _registry():
    """Fresh SkillsRegistry with isolated ``tools.*`` import.

    Defensive cleanup: tests/test_summary_triggers.py stubs `tools.skills.registry`
    in sys.modules at import time. Pop any stale entries before our import so
    the test works both standalone and in the full suite. On exit, restore the
    previous ``tools.*`` graph so later tests keep their original module objects
    (avoids stale class refs vs monkeypatch on a re-imported LocalFsBackend).
    """
    for _src in PACKAGE_SRC_DIRS:
        sys.path.insert(0, str(_src))
    saved = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == "gc_core.tools" or name.startswith("gc_core.tools.")
    }
    try:
        from gc_core.tools.skills.registry import SkillsRegistry

        yield SkillsRegistry(skills_root=str(ASSETS / "skills"))
    finally:
        for name in list(sys.modules):
            if name == "gc_core.tools" or name.startswith("gc_core.tools."):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


def _sample_skill() -> tuple[Path, str]:
    """Pick a deterministic existing skill dir (with SKILL.md), pack-agnostically.

    Globs every skills/**/SKILL.md (flat skill, pack bootstrap, or pack inner),
    excludes the skill-creator skill itself (self-packaging quirks), takes the
    first sorted candidate. skill_id is computed by package_skill's own
    _extract_skill_id so skill-<id>.zip / <id>/SKILL.md assertions stay
    consistent with the script regardless of flat-vs-pack layout.
    """
    sys.path.insert(0, str(SKILL_DIR / "scripts"))
    import package_skill as _ps  # noqa: E402

    for cand in sorted((ASSETS / "skills").glob("**/SKILL.md")):
        skill_dir = cand.parent
        if skill_dir == SKILL_DIR or SKILL_DIR in skill_dir.parents:
            continue
        return skill_dir, _ps._extract_skill_id(skill_dir)
    raise AssertionError("no sample skill with SKILL.md found under skills/")


def test_skill_creator_loads_via_registry():
    """skill-creator must be discoverable in the registry with correct metadata.

    In the lean version we don't ship references/ — so detail['references']
    should be empty.
    """
    with _registry() as registry:
        detail = registry.load_skill_detail("skill-creator")

        assert detail["id"] == "skill-creator"
        assert detail["name"]
        assert "skill" in detail["description"].lower()
        assert detail["instructions"], "SKILL.md body must not be empty"
        assert detail["has_scripts"] is True
        assert detail["references"] == [], detail["references"]


def test_quick_validate_runs_on_existing_skill():
    """quick_validate.py must validate an existing project skill (after the Task 3 adaptations)."""
    sample_dir, _sample_id = _sample_skill()
    assert sample_dir.is_dir()

    result = subprocess.run(
        [sys.executable, "-m", "scripts.quick_validate", str(sample_dir.resolve())],
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
    sample_dir, sample_id = _sample_skill()
    assert sample_dir.is_dir()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.package_skill",
            str(sample_dir.resolve()),
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
    assert skill_zip.name == f"skill-{sample_id}.zip", skill_zip.name

    import zipfile
    with zipfile.ZipFile(skill_zip) as zf:
        names = zf.namelist()
        assert f"{sample_id}/SKILL.md" in names, names


def test_package_skill_includes_readme_with_metadata(tmp_path):
    """Archive must contain README.md at root with id, version, description from frontmatter."""
    sample_dir, sample_id = _sample_skill()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.package_skill",
            str(sample_dir.resolve()),
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
        assert sample_id in readme_content
        assert "Как установить" in readme_content
        assert "demo/" in readme_content or "Что в архиве" in readme_content


def test_package_skill_explicit_source_layer(tmp_path):
    """--source-layer flag should propagate into README."""
    sample_dir, _sample_id = _sample_skill()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.package_skill",
            str(sample_dir.resolve()),
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


@pytest.mark.parametrize("base, skill_subpath, expected_layer", [
    # Project layer: under <cwd>/.gc/skills/
    ("repo", ".gc/skills/test-skill", "project"),
    # System layer: под каталогом скиллов ДИСТРИБУТИВА. До разбиения на пакеты
    # это был <repo>/skills/, теперь ассеты принадлежат gc-core.
    ("assets", "skills/test-skill", "system"),
])
def test_package_skill_auto_detects_source_layer(tmp_path, base, skill_subpath, expected_layer):
    """Without --source-layer, README should auto-detect from path."""
    root = PROJECT_ROOT if base == "repo" else ASSETS
    skill_dir = root / skill_subpath
    cleanup_needed = not skill_dir.exists()
    # Самый верхний каталог, которого ещё нет: mkdir(parents=True) создаёт всю
    # цепочку, а убирать её надо целиком. Иначе после каждого прогона в рабочей
    # копии оседает пустой `<корень>/.gc/skills/` — тест мусорит в дереве
    # проекта (наблюдалось: packages/core/.gc и <repo>/.gc).
    created_root = skill_dir
    while created_root.parent != root and not created_root.parent.exists():
        created_root = created_root.parent
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
            shutil.rmtree(created_root, ignore_errors=True)


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

    # Полный _dev/ — gate в package_skill требует все required artifacts
    _make_complete_dev_dir(skill_dir)

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
    dev_dir = _make_complete_dev_dir(skill_dir)

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
    dev_dir = _make_complete_dev_dir(skill_dir)

    out_dir = tmp_path / "out"
    out_dir.mkdir()

    # Direct import approach (subprocess can't be easily monkeypatched):
    sys.path.insert(0, str(ASSETS / "skills" / "skill-creator" / "scripts"))
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


def test_package_skill_refuses_incomplete_dev(tmp_path):
    """Если в _dev/ есть intent_brief, но не хватает других required — упаковка fails."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nid: test-skill\nname: test-skill\ndescription: Test\n---\n# Test\n",
        encoding="utf-8",
    )
    # Только intent_brief — нет completion_report, нет examples
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
    # Должен fail с понятным сообщением
    assert result.returncode != 0, (
        f"Expected non-zero exit, got {result.returncode}. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = (result.stderr or "") + (result.stdout or "")
    assert "completion_report.md" in combined, combined
    assert "examples/prompt_01.md" in combined or "prompt_01" in combined, combined
    # Архив НЕ должен быть создан
    assert not list(out_dir.glob("skill-*.zip")), "archive should NOT exist"


def test_package_skill_passes_with_complete_dev(tmp_path):
    """Полный _dev/ (все 6 required файлов) — упаковка проходит, demo/ в архиве."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nid: test-skill\nname: test-skill\ndescription: Test\n---\n# Test\n",
        encoding="utf-8",
    )
    _make_complete_dev_dir(skill_dir)

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
        assert "demo/intent_brief.md" in names, names
        assert "demo/completion_report.md" in names, names
        assert "demo/examples/prompt_01_output.md" in names, names
        assert "demo/examples/prompt_02_output.md" in names, names


def test_package_skill_no_intent_brief_skips_validation(tmp_path):
    """Если _dev/intent_brief.md нет — gate не срабатывает, упаковка без demo/."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nid: test-skill\nname: test-skill\ndescription: Test\n---\n# Test\n",
        encoding="utf-8",
    )
    # _dev/ существует но пуст — flow не запущен
    dev_dir = skill_dir / "_dev"
    dev_dir.mkdir()
    (dev_dir / "unrelated.md").write_text("# something\n", encoding="utf-8")

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
    # Должен пройти (intent_brief нет — gate skipped)
    assert result.returncode == 0, (result.stdout, result.stderr)
    skill_zip = next(out_dir.glob("skill-*.zip"))
    # demo/ всё равно может быть (т.к. _dev есть и есть unrelated.md внутри —
    # это попадает в demo/, но gate не активируется)
    # Главное — упаковка не fails из-за missing required files.
