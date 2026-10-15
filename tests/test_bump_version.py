"""Скрипт повышения версий видит новый дистрибутив gc-a2a."""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _bump():
    path = REPO / "scripts" / "bump_version.py"
    spec = importlib.util.spec_from_file_location("bump_version", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pin_pattern_accepts_gc_a2a():
    bump = _bump()
    matched = bump.PIN_RE.search('"gc-a2a==0.4.0"')
    assert matched is not None
    assert matched.group(1) == "gc-a2a"
    assert bump.PIN_RE.search('"gc-core==1.2.3"').group(1) == "gc-core"


def test_dry_run_bumps_a2a_manifest_and_core_pin():
    bump = _bump()
    manifest = REPO / "packages" / "a2a" / "pyproject.toml"
    assert manifest in bump.MANIFESTS
    before = manifest.read_text(encoding="utf-8")
    changes = bump._bump_manifest(manifest, "9.9.9", True)
    changes += bump._bump_pins(manifest, "9.9.9", True)
    root_changes = bump._bump_pins(REPO / "pyproject.toml", "9.9.9", True)
    assert manifest.read_text(encoding="utf-8") == before
    text = "\n".join(changes + root_changes)
    assert "packages/a2a/pyproject.toml: version 0.4.0 → 9.9.9" in text
    assert "packages/a2a/pyproject.toml: gc-core 0.4.0 → 9.9.9" in text
    assert "pyproject.toml: gc-a2a 0.4.0 → 9.9.9" in text
