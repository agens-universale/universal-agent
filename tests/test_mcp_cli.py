from pathlib import Path

import pytest

from config_modules import env_vars


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {"config": tmp_path / "cfg",
        "data": tmp_path / "d", "cache": tmp_path / "cache", "state": tmp_path / "s"})
    return tmp_path


def test_install_by_name_installs_and_wires(fake_env, monkeypatch):
    from mcp_runtime import cli
    from recommended_mcp.manifest import MCPEntry, InstallSpec

    office = MCPEntry(name="office", path=Path("recommended_mcp/office"),
                     install=InstallSpec(type="source", source=Path("../external/office-mcp")))
    monkeypatch.setattr(cli, "_load_manifest_entries", lambda: ([office], Path(".")))
    installed = []
    monkeypatch.setattr(cli, "_install_one", lambda e, root, dep: installed.append(e.name) or True)
    wired = []
    monkeypatch.setattr(cli, "_wire_one", lambda e, root, dep: wired.append(e.name))

    rc = cli.main(["install", "office"])
    assert rc == 0
    assert installed == ["office"] and wired == ["office"]


def test_install_unknown_name_errors(fake_env, monkeypatch):
    from mcp_runtime import cli
    monkeypatch.setattr(cli, "_load_manifest_entries", lambda: ([], Path(".")))
    rc = cli.main(["install", "nope"])
    assert rc != 0


def test_install_all_wires_only_installed(fake_env, monkeypatch):
    from mcp_runtime import cli
    from recommended_mcp.manifest import MCPEntry, InstallSpec
    from pathlib import Path
    good = MCPEntry(name="good", path=Path("."), install=InstallSpec(type="source"))
    bad = MCPEntry(name="bad", path=Path("."), install=InstallSpec(type="source"))
    monkeypatch.setattr(cli, "_load_manifest_entries", lambda: ([good, bad], Path(".")))
    monkeypatch.setattr(cli, "_install_one", lambda e, root, dep: e.name == "good")
    wired = []
    monkeypatch.setattr(cli, "_wire_one", lambda e, root, dep: wired.append(e.name))
    rc = cli.main(["install"])   # no name → all
    assert rc == 0
    assert wired == ["good"]      # bad failed install → not wired


def test_remove_unwires(fake_env, monkeypatch):
    from mcp_runtime import cli, wiring
    from recommended_mcp.manifest import MCPEntry, InstallSpec
    office = MCPEntry(name="office", path=Path("."), install=InstallSpec(type="source"))
    monkeypatch.setattr(cli, "_load_manifest_entries", lambda: ([office], Path(".")))
    monkeypatch.setattr(cli, "_uninstall_one", lambda e, root: None)
    (fake_env / "cfg").mkdir(parents=True, exist_ok=True)
    (fake_env / "cfg" / "config.local.yml").write_text(
        "mcp:\n  servers:\n    office:\n      transport: stdio\n", encoding="utf-8")

    rc = cli.main(["remove", "office"])
    assert rc == 0
    assert wiring.wired_servers() == {}
