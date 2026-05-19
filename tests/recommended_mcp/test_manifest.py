"""Unit-тесты для парсера recommended_mcp/config.yml."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from recommended_mcp.manifest import (
    InstallSpec,
    ManifestError,
    RunSpec,
    default_agent_dep_dir,
    load_agent_dep_dir,
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


def test_source_mcp(tmp_path: Path) -> None:
    f = _write(tmp_path, """
        mcps:
          - name: computer
            path: ./computer
            install:
              type: source
              source: ../external/computer-mcp
              platforms: [win32]
    """)
    entry = load_manifest(f)[0]
    assert entry.install.type == "source"
    assert entry.install.source == Path("../external/computer-mcp")
    assert entry.install.platforms == ("win32",)


def test_source_without_source_field_raises(tmp_path: Path) -> None:
    f = _write(tmp_path, """
        mcps:
          - name: x
            path: ./x
            install:
              type: source
    """)
    with pytest.raises(ManifestError, match="install.type=source requires install.source"):
        load_manifest(f)


def test_load_agent_dep_dir_default_uses_gc_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _write(tmp_path, "mcps: []\n")
    fake_data = tmp_path / "share" / "gc"
    monkeypatch.setattr(
        "config_modules.env_vars.gc_paths",
        lambda: {
            "state": tmp_path / "state" / "gc",
            "config": tmp_path / "config" / "gc",
            "cache": tmp_path / "cache" / "gc",
            "data": fake_data,
        },
    )
    assert load_agent_dep_dir(f) == fake_data.resolve()


def test_load_agent_dep_dir_explicit_override(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        agent_dep_dir: /opt/my-node
        mcps: []
        """,
    )
    assert load_agent_dep_dir(f) == Path("/opt/my-node")


def test_default_agent_dep_dir_matches_gc_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_data = tmp_path / "data" / "gc"
    monkeypatch.setattr(
        "config_modules.env_vars.gc_paths",
        lambda: {
            "state": tmp_path / "state",
            "config": tmp_path / "config",
            "cache": tmp_path / "cache",
            "data": fake_data,
        },
    )
    assert default_agent_dep_dir() == fake_data


def test_source_field_ignored_for_non_source_types(tmp_path: Path) -> None:
    # Терпим source: в блоке wheel — это не ломка, просто избыточно.
    # Главное чтобы не падало.
    f = _write(tmp_path, """
        mcps:
          - name: x
            path: ./x
            install:
              type: wheel
              source: ../whatever
    """)
    entry = load_manifest(f)[0]
    assert entry.install.type == "wheel"
    assert entry.install.source == Path("../whatever")
