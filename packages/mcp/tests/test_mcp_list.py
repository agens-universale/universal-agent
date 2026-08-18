"""_installed_status: трёхзначный статус INSTALLED для gc-mcp list."""
from pathlib import Path

from recommended_mcp.runtime import cli
from recommended_mcp.manifest import MCPEntry, InstallSpec


def _src(dist=None):
    return MCPEntry(name="office", path=Path("."),
                    install=InstallSpec(type="source", source=Path("../external/office-mcp"), dist=dist))


def test_status_source_installed(monkeypatch):
    monkeypatch.setattr("recommended_mcp.install._dist_name", lambda e, r: "office-mcp")
    monkeypatch.setattr("recommended_mcp.runtime.venv.query_installed_version", lambda n: "1.0")
    assert cli._installed_status(_src("office-mcp"), Path("."), Path(".")) == "yes"


def test_status_source_not_installed(monkeypatch):
    monkeypatch.setattr("recommended_mcp.install._dist_name", lambda e, r: "office-mcp")
    monkeypatch.setattr("recommended_mcp.runtime.venv.query_installed_version", lambda n: None)
    assert cli._installed_status(_src("office-mcp"), Path("."), Path(".")) == "—"


def test_status_source_unknown_name(monkeypatch):
    monkeypatch.setattr("recommended_mcp.install._dist_name", lambda e, r: None)
    assert cli._installed_status(_src(), Path("."), Path(".")) == "?"


def test_status_npx_present(monkeypatch):
    monkeypatch.setattr("recommended_mcp.install.npm_global_has", lambda pkg, dep: True)
    e = MCPEntry(name="pw", path=Path("."),
                 install=InstallSpec(type="npx", npm_package="@playwright/mcp"))
    assert cli._installed_status(e, Path("."), Path(".")) == "yes"


def test_status_npx_undetermined(monkeypatch):
    monkeypatch.setattr("recommended_mcp.install.npm_global_has", lambda pkg, dep: None)
    e = MCPEntry(name="pw", path=Path("."),
                 install=InstallSpec(type="npx", npm_package="@playwright/mcp"))
    assert cli._installed_status(e, Path("."), Path(".")) == "?"


def test_status_npx_not_installed(monkeypatch):
    monkeypatch.setattr("recommended_mcp.install.npm_global_has", lambda pkg, dep: False)
    e = MCPEntry(name="pw", path=Path("."),
                 install=InstallSpec(type="npx", npm_package="@playwright/mcp"))
    assert cli._installed_status(e, Path("."), Path(".")) == "—"


def test_install_all_skips_optional(monkeypatch):
    from recommended_mcp.runtime import cli as cli_mod
    from recommended_mcp.manifest import MCPEntry, InstallSpec
    from pathlib import Path

    entries = [
        MCPEntry("keep", Path("./keep"), InstallSpec(type="source", source=Path("../x")), optional=False),
        MCPEntry("skip", Path("./skip"), InstallSpec(type="source", source=Path("../y")), optional=True),
    ]
    monkeypatch.setattr(cli_mod, "_load_manifest_entries", lambda config=None: (entries, Path(".")))
    monkeypatch.setattr(cli_mod, "_agent_dep_dir", lambda: Path("."))
    monkeypatch.setattr(cli_mod, "_load_repo_url", lambda config=None: (None, None))
    installed = []
    monkeypatch.setattr(cli_mod, "_install_one", lambda e, *a, **k: (installed.append(e.name) or True))
    monkeypatch.setattr(cli_mod, "_wire_one", lambda *a, **k: None)

    cli_mod._cmd_install(None)   # install-all
    assert installed == ["keep"]   # optional 'skip' пропущен
