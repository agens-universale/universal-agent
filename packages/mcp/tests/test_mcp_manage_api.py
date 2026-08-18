from gc_core.mcp_runtime import cli as cli


def test_wire_unknown_name_returns_false(monkeypatch):
    monkeypatch.setattr(cli, "_load_manifest_entries", lambda config=None: ([], "root"))
    assert cli.wire("nope") is False


def test_wire_known_name_calls_wire_one(monkeypatch):
    class E:  # noqa: D401 - fake manifest entry
        name = "office"
    calls = {}
    monkeypatch.setattr(cli, "_load_manifest_entries", lambda config=None: ([E()], "root"))
    monkeypatch.setattr(cli, "_agent_dep_dir", lambda: "dep")
    monkeypatch.setattr(cli, "_wire_one", lambda entry, root, dep: calls.setdefault("wired", entry.name))
    assert cli.wire("office") is True
    assert calls["wired"] == "office"


def test_unwire_delegates_to_wiring(monkeypatch):
    from gc_core.mcp_runtime import wiring as wiring
    monkeypatch.setattr(wiring, "unwire", lambda name: name == "office")
    assert cli.unwire("office") is True
    assert cli.unwire("ghost") is False


def test_remove_delegates_to_cmd_remove(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_cmd_remove", lambda name, keep: seen.update(name=name, keep=keep) or 0)
    assert cli.remove("office", keep_package=True) == 0
    assert seen == {"name": "office", "keep": True}


def test_installed_state_not_installed(monkeypatch):
    class E:
        name = "office"
        class install:  # noqa: N801
            type = "source"
            package = None
    monkeypatch.setattr(cli, "_load_manifest_entries", lambda config=None: ([E()], "root"))
    monkeypatch.setattr(cli, "_agent_dep_dir", lambda: "dep")
    monkeypatch.setattr(cli, "_installed_status", lambda e, r, d: "—")
    st = cli.installed_state("office")
    assert st == {"installed": False, "version": None, "update": None}


def test_installed_state_installed_with_version(monkeypatch):
    class E:
        name = "office"
        path = "recommended_mcp/office"
        class install:  # noqa: N801
            type = "pypi"
            package = "office-mcp==0.3.1"
    monkeypatch.setattr(cli, "_load_manifest_entries", lambda config=None: ([E()], "root"))
    monkeypatch.setattr(cli, "_agent_dep_dir", lambda: "dep")
    monkeypatch.setattr(cli, "_installed_status", lambda e, r, d: "yes")
    import recommended_mcp.install as inst
    from gc_core.mcp_runtime import venv as venv
    monkeypatch.setattr(inst, "_dist_name", lambda e, root: "office-mcp")
    monkeypatch.setattr(venv, "query_installed_version", lambda d: "0.3.1")
    st = cli.installed_state("office")
    assert st["installed"] is True and st["version"] == "0.3.1"


def test_manual_server_names_reads_runtime_key():
    cfg = {"mcp-servers": {"corp-jira": {}, "corp-confluence": {}}}
    assert sorted(cli.manual_server_names(cfg)) == ["corp-confluence", "corp-jira"]
    assert cli.manual_server_names(None) == []


def _pypi_entry(package):
    class E:
        name = "x"
        class install:  # noqa: N801
            type = "pypi"
    E.install.package = package
    return E


def test_available_update_pin_newer_than_installed():
    assert cli._available_update(_pypi_entry("ddgs-mcp==0.2.0"), "0.1.0") == "0.2.0"


def test_available_update_pin_equals_installed_is_none():
    assert cli._available_update(_pypi_entry("ddgs-mcp==0.2.0"), "0.2.0") is None


def test_available_update_source_type_is_none():
    class E:
        class install:  # noqa: N801
            type = "source"
            package = None
    assert cli._available_update(E, "0.1.0") is None


def test_available_update_no_installed_is_none():
    assert cli._available_update(_pypi_entry("ddgs-mcp==0.2.0"), None) is None
