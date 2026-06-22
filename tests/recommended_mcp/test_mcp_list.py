"""_installed_status: трёхзначный статус INSTALLED для gc-mcp list."""
from pathlib import Path

from mcp_runtime import cli
from recommended_mcp.manifest import MCPEntry, InstallSpec


def _src(dist=None):
    return MCPEntry(name="office", path=Path("."),
                    install=InstallSpec(type="source", source=Path("../external/office-mcp"), dist=dist))


def test_status_source_installed(monkeypatch):
    monkeypatch.setattr("recommended_mcp.install._dist_name", lambda e, r: "office-mcp")
    monkeypatch.setattr("mcp_runtime.venv.query_installed_version", lambda n: "1.0")
    assert cli._installed_status(_src("office-mcp"), Path("."), Path(".")) == "yes"


def test_status_source_not_installed(monkeypatch):
    monkeypatch.setattr("recommended_mcp.install._dist_name", lambda e, r: "office-mcp")
    monkeypatch.setattr("mcp_runtime.venv.query_installed_version", lambda n: None)
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
