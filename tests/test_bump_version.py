"""Скрипт bump_version покрывает все манифесты workspace и их gc-* пины."""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NEW = "9.9.9"


def _bump():
    path = REPO / "scripts" / "bump_version.py"
    spec = importlib.util.spec_from_file_location("bump_version", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pin_pattern_accepts_workspace_packages():
    bump = _bump()
    for pkg in ("gc-a2a", "gc-core", "gc-core-temporalio"):
        matched = bump.PIN_RE.search(f'"{pkg}==1.2.3"')
        assert matched is not None
        assert matched.group(1) == pkg


def test_manifests_cover_all_package_pyprojects():
    bump = _bump()
    expected = {REPO / "pyproject.toml"}
    expected.update((REPO / "packages").glob("*/pyproject.toml"))
    assert set(bump.MANIFESTS) == expected


def test_dry_run_bumps_all_manifests_from_disk():
    bump = _bump()
    snapshots = {p: p.read_text(encoding="utf-8") for p in bump.MANIFESTS}

    expected: list[str] = []
    for path, text in snapshots.items():
        rel = path.relative_to(REPO)
        for m in bump.VERSION_RE.finditer(text):
            old = m.group(2)
            if old != NEW:
                expected.append(f"  {rel}: version {old} → {NEW}")
        for m in bump.PIN_RE.finditer(text):
            pkg = m.group(1)
            old = m.group(0).split("==")[1].rstrip('"')
            if old != NEW:
                expected.append(f"  {rel}: {pkg} {old} → {NEW}")

    actual: list[str] = []
    for path in bump.MANIFESTS:
        actual += bump._bump_manifest(path, NEW, True)
        actual += bump._bump_pins(path, NEW, True)

    assert actual == expected
    assert expected, "ожидали хотя бы одно изменение версии/пина"
    for path, before in snapshots.items():
        assert path.read_text(encoding="utf-8") == before
