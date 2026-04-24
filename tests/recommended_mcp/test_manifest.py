"""Unit-тесты для парсера recommended_mcp/config.yml."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from recommended_mcp.manifest import (
    InstallSpec,
    ManifestError,
    RunSpec,
    load_manifest,
)


def _write(tmp_path: Path, content: str) -> Path:
    f = tmp_path / "config.yml"
    f.write_text(dedent(content), encoding="utf-8")
    return f


def test_wheel_mcp_without_run_block(tmp_path: Path) -> None:
    f = _write(tmp_path, """
        mcps:
          - name: computer
            path: ./computer
            install:
              type: wheel
              platforms: [win32]
    """)
    manifest = load_manifest(f)
    assert len(manifest) == 1
    entry = manifest[0]
    assert entry.name == "computer"
    assert entry.path == Path("./computer")
    assert entry.install == InstallSpec(type="wheel", platforms=("win32",))
    assert entry.run is None


def test_docker_mcp_with_run_block(tmp_path: Path) -> None:
    f = _write(tmp_path, """
        mcps:
          - name: office-reader
            path: ./office-reader
            install:
              type: docker
            run:
              type: docker-compose
              detach: true
    """)
    entry = load_manifest(f)[0]
    assert entry.install == InstallSpec(type="docker", platforms=())
    assert entry.run == RunSpec(type="docker-compose", detach=True)


def test_multiple_mcps(tmp_path: Path) -> None:
    f = _write(tmp_path, """
        mcps:
          - name: a
            path: ./a
            install: {type: wheel}
          - name: b
            path: ./b
            install: {type: docker}
            run: {type: docker-compose, detach: false}
    """)
    entries = load_manifest(f)
    assert [e.name for e in entries] == ["a", "b"]


def test_missing_name_raises(tmp_path: Path) -> None:
    f = _write(tmp_path, """
        mcps:
          - path: ./x
            install: {type: wheel}
    """)
    with pytest.raises(ManifestError, match="missing 'name'"):
        load_manifest(f)


def test_missing_install_block_raises(tmp_path: Path) -> None:
    f = _write(tmp_path, """
        mcps:
          - name: x
            path: ./x
    """)
    with pytest.raises(ManifestError, match="missing 'install'"):
        load_manifest(f)


def test_unknown_top_level_key_is_ignored(tmp_path: Path) -> None:
    # Чтобы Никита мог добавлять свои поля без поломки для нас.
    f = _write(tmp_path, """
        version: 1
        mcps:
          - name: x
            path: ./x
            install: {type: wheel}
    """)
    assert len(load_manifest(f)) == 1


def test_empty_mcps_list(tmp_path: Path) -> None:
    f = _write(tmp_path, "mcps: []\n")
    assert load_manifest(f) == []


def test_run_without_type_raises(tmp_path: Path) -> None:
    f = _write(tmp_path, """
        mcps:
          - name: x
            path: ./x
            install: {type: docker}
            run:
              detach: true
    """)
    with pytest.raises(ManifestError, match="missing 'run.type'"):
        load_manifest(f)


def test_install_without_type_raises(tmp_path: Path) -> None:
    f = _write(tmp_path, """
        mcps:
          - name: x
            path: ./x
            install:
              platforms: [win32]
    """)
    with pytest.raises(ManifestError, match="missing 'install.type'"):
        load_manifest(f)
