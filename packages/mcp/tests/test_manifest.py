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
    default_manifest_path,
    load_agent_dep_dir,
    load_manifest,
    resolve_manifest_path,
)


def _write(tmp_path: Path, content: str) -> Path:
    f = tmp_path / "config.yml"
    f.write_text(dedent(content), encoding="utf-8")
    return f


def test_default_manifest_path(tmp_path: Path) -> None:
    root = tmp_path / "recommended_mcp"
    root.mkdir()
    assert default_manifest_path(root) == root / "config.yml"


def test_resolve_manifest_path_default(tmp_path: Path) -> None:
    root = tmp_path / "recommended_mcp"
    root.mkdir()
    default = root / "config.yml"
    default.write_text("mcps: []\n", encoding="utf-8")
    assert resolve_manifest_path(root, None) == default.resolve()


def test_resolve_manifest_path_custom_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "recommended_mcp"
    root.mkdir()
    custom = root / "config.local.yml"
    custom.write_text("mcps: []\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert (
        resolve_manifest_path(root, Path("recommended_mcp/config.local.yml"))
        == custom.resolve()
    )


def test_resolve_manifest_path_missing_raises(tmp_path: Path) -> None:
    root = tmp_path / "recommended_mcp"
    root.mkdir()
    with pytest.raises(ManifestError, match="manifest not found"):
        resolve_manifest_path(root, tmp_path / "nope.yml")


def test_wheel_mcp_without_run_block(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        mcps:
          - name: computer
            path: ./computer
            install:
              type: wheel
              platforms: [win32]
    """,
    )
    manifest = load_manifest(f)
    assert len(manifest) == 1
    entry = manifest[0]
    assert entry.name == "computer"
    assert entry.path == Path("./computer")
    assert entry.install == InstallSpec(type="wheel", platforms=("win32",))
    assert entry.run is None


def test_docker_mcp_with_run_block(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        mcps:
          - name: my-docker-mcp
            path: ./my-docker-mcp
            install:
              type: docker
            run:
              type: docker-compose
              detach: true
    """,
    )
    entry = load_manifest(f)[0]
    assert entry.install == InstallSpec(type="docker", platforms=())
    assert entry.run == RunSpec(type="docker-compose", detach=True)


def test_multiple_mcps(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        mcps:
          - name: a
            path: ./a
            install: {type: wheel}
          - name: b
            path: ./b
            install: {type: docker}
            run: {type: docker-compose, detach: false}
    """,
    )
    entries = load_manifest(f)
    assert [e.name for e in entries] == ["a", "b"]


def test_missing_name_raises(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        mcps:
          - path: ./x
            install: {type: wheel}
    """,
    )
    with pytest.raises(ManifestError, match="missing 'name'"):
        load_manifest(f)


def test_missing_install_block_raises(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        mcps:
          - name: x
            path: ./x
    """,
    )
    with pytest.raises(ManifestError, match="missing 'install'"):
        load_manifest(f)


def test_unknown_top_level_key_is_ignored(tmp_path: Path) -> None:
    # Чтобы Никита мог добавлять свои поля без поломки для нас.
    f = _write(
        tmp_path,
        """
        version: 1
        mcps:
          - name: x
            path: ./x
            install: {type: wheel}
    """,
    )
    assert len(load_manifest(f)) == 1


def test_empty_mcps_list(tmp_path: Path) -> None:
    f = _write(tmp_path, "mcps: []\n")
    assert load_manifest(f) == []


def test_run_without_type_raises(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        mcps:
          - name: x
            path: ./x
            install: {type: docker}
            run:
              detach: true
    """,
    )
    with pytest.raises(ManifestError, match="missing 'run.type'"):
        load_manifest(f)


def test_install_without_type_raises(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        mcps:
          - name: x
            path: ./x
            install:
              platforms: [win32]
    """,
    )
    with pytest.raises(ManifestError, match="missing 'install.type'"):
        load_manifest(f)


def test_source_mcp(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        mcps:
          - name: computer
            path: ./computer
            install:
              type: source
              source: ../external/computer-mcp
              platforms: [win32]
    """,
    )
    entry = load_manifest(f)[0]
    assert entry.install.type == "source"
    assert entry.install.source == Path("../external/computer-mcp")
    assert entry.install.platforms == ("win32",)


def test_command_run_block(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        mcps:
          - name: science
            path: ./science
            install: {type: source, source: ../external/science-mcp}
            run:
              type: command
              command: science-mcp
              args: ["--transport", "http"]
              env:
                SCIENCE_MCP_PORT: "8765"
    """,
    )
    entry = load_manifest(f)[0]
    assert entry.run is not None
    assert entry.run.type == "command"
    assert entry.run.command == "science-mcp"
    assert entry.run.args == ("--transport", "http")
    assert entry.run.env == (("SCIENCE_MCP_PORT", "8765"),)


def test_command_without_command_raises(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        mcps:
          - name: science
            path: ./science
            install: {type: source, source: ../external/science-mcp}
            run:
              type: command
    """,
    )
    with pytest.raises(
        ManifestError, match="run.type=command requires run.command"
    ):
        load_manifest(f)


def test_pypi_mcp(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        mcps:
          - name: duckduckgo_search
            path: ./duckduckgo_search
            install:
              type: pypi
              package: ddgs-mcp==0.1.0
    """,
    )
    entry = load_manifest(f)[0]
    assert entry.install.type == "pypi"
    assert entry.install.package == "ddgs-mcp==0.1.0"
    assert entry.run is None


def test_pypi_without_package_raises(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        mcps:
          - name: duckduckgo_search
            path: ./duckduckgo_search
            install:
              type: pypi
    """,
    )
    with pytest.raises(
        ManifestError, match="install.type=pypi requires install.package"
    ):
        load_manifest(f)


def test_npx_commands_are_parsed(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        mcps:
          - name: browser_playwright
            path: ./browser_playwright
            install:
              type: npx
              commands:
                - [npm, install, -g, "@playwright/mcp@0.0.70"]
                - [npx, -y, playwright, install, chromium]
    """,
    )
    entry = load_manifest(f)[0]
    assert entry.install.commands == (
        ("npm", "install", "-g", "@playwright/mcp@0.0.70"),
        ("npx", "-y", "playwright", "install", "chromium"),
    )


def test_source_without_source_field_raises(tmp_path: Path) -> None:
    f = _write(
        tmp_path,
        """
        mcps:
          - name: x
            path: ./x
            install:
              type: source
    """,
    )
    with pytest.raises(
        ManifestError, match="install.type=source requires install.source"
    ):
        load_manifest(f)


def test_load_agent_dep_dir_default_uses_gc_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _write(tmp_path, "mcps: []\n")
    fake_data = tmp_path / "share" / "gc"
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.gc_paths",
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
        "gc_core.config_modules.env_vars.gc_paths",
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
    f = _write(
        tmp_path,
        """
        mcps:
          - name: x
            path: ./x
            install:
              type: wheel
              source: ../whatever
    """,
    )
    entry = load_manifest(f)[0]
    assert entry.install.type == "wheel"
    assert entry.install.source == Path("../whatever")


def test_install_spec_parses_npm_package(tmp_path):
    from recommended_mcp.manifest import load_manifest

    root = tmp_path / "recommended_mcp"
    root.mkdir()
    (root / "config.yml").write_text(
        "mcps:\n"
        "  - name: pw\n"
        "    path: ./pw\n"
        "    install:\n"
        "      type: npx\n"
        "      npm_package: '@playwright/mcp'\n"
        "      commands: [[npm, install, -g, '@playwright/mcp@1.0.0']]\n",
        encoding="utf-8",
    )
    entries = load_manifest(root / "config.yml")
    assert entries[0].install.npm_package == "@playwright/mcp"


def test_npx_mcp_parses_install_commands(tmp_path: Path) -> None:
    # Баг-регрессия: install.commands (warmup npx/npm) раньше терялись в парсере.
    f = _write(
        tmp_path,
        """
        mcps:
          - name: browser_playwright
            path: ./browser_playwright
            install:
              type: npx
              commands:
                - [npm, "install", "-g", "@playwright/mcp@0.0.70"]
                - [npx, "-y", "playwright", "install", "chromium"]
    """,
    )
    entry = load_manifest(f)[0]
    assert entry.install.type == "npx"
    assert entry.install.commands == (
        ("npm", "install", "-g", "@playwright/mcp@0.0.70"),
        ("npx", "-y", "playwright", "install", "chromium"),
    )


def test_load_repo_url(tmp_path):
    from recommended_mcp.manifest import load_repo_url

    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "repo_url: https://example.org/repo\nrepo_ref: v1\nmcps: []\n",
        encoding="utf-8",
    )
    assert load_repo_url(cfg) == ("https://example.org/repo", "v1")
    cfg2 = tmp_path / "c2.yml"
    cfg2.write_text("mcps: []\n", encoding="utf-8")
    assert load_repo_url(cfg2) == (None, None)


def test_source_mcp_with_dist(tmp_path):
    f = _write(
        tmp_path,
        """
        mcps:
          - name: office
            path: ./office
            install:
              type: source
              source: ../external/office-mcp
              dist: office-mcp
    """,
    )
    assert load_manifest(f)[0].install.dist == "office-mcp"


def test_install_dist_defaults_none(tmp_path):
    f = _write(
        tmp_path,
        """
        mcps:
          - name: science
            path: ./science
            install: {type: source, source: ../external/science-mcp}
    """,
    )
    assert load_manifest(f)[0].install.dist is None


def test_every_source_entry_declares_dist():
    """Каждая source-запись бандл-манифеста обязана нести install.dist."""
    from recommended_mcp.manifest import default_manifest_path, load_manifest
    from recommended_mcp.runtime.manifest_resolve import recipe_root

    entries = load_manifest(default_manifest_path(recipe_root()))
    for e in entries:
        if e.install.type == "source":
            assert e.install.dist, (
                f"{e.name}: source-запись обязана иметь install.dist"
            )


def test_manifest_parses_optional_flag(tmp_path):
    from recommended_mcp.manifest import load_manifest

    p = tmp_path / "config.yml"
    p.write_text(
        "mcps:\n"
        "  - name: a\n"
        "    path: ./a\n"
        "    optional: true\n"
        "    install: {type: source, source: ../x, dist: x}\n"
        "  - name: b\n"
        "    path: ./b\n"
        "    install: {type: source, source: ../y, dist: y}\n",
        encoding="utf-8",
    )
    entries = load_manifest(p)
    by = {e.name: e for e in entries}
    assert by["a"].optional is True
    assert by["b"].optional is False


def test_manifest_dist_matches_pyproject_when_source_present():
    """Где source-папка на диске есть — install.dist == [project].name из pyproject.

    Существование папки проверяется ТЕМ ЖЕ ``_vendor_candidates``, которым её
    ищет установщик. Раньше здесь была своя арифметика ``root / install.source``
    (root — каталог рецептов), она не попадала в цель ни разу, цикл всегда делал
    ``continue``, и тест был вакуумным: он проходил бы и при ``_dist_name``,
    всегда возвращающей None. Счётчик ниже стережёт именно это.
    """
    import dataclasses

    from recommended_mcp import install as inst
    from recommended_mcp.manifest import default_manifest_path, load_manifest
    from recommended_mcp.runtime.manifest_resolve import recipe_root

    root = recipe_root()
    checked = 0
    for e in load_manifest(default_manifest_path(root)):
        if e.install.type == "source" and e.install.dist and e.install.source:
            name = inst._source_dir_name(e.install.source)
            if not any(
                (c / "pyproject.toml").is_file()
                for c in inst._vendor_candidates(name, root)
            ):
                continue
            checked += 1
            no_dist = dataclasses.replace(
                e, install=dataclasses.replace(e.install, dist=None)
            )
            assert e.install.dist == inst._dist_name(no_dist, root), (
                f"{e.name}: install.dist={e.install.dist} != имя из pyproject"
            )
    assert checked, (
        "ни одной source-папки не найдено — тест вакуумен; проверьте, что "
        "external/vendor/* на месте и _vendor_candidates их находит"
    )
