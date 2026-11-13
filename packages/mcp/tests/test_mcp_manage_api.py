from gc_testkit.settings import make_settings
from recommended_mcp.runtime import cli as cli


def test_wire_unknown_name_returns_false(monkeypatch):
    monkeypatch.setattr(
        cli, "_load_manifest_entries", lambda config=None: ([], "root")
    )
    assert cli.wire("nope") is False


def test_wire_known_name_calls_wire_one(monkeypatch):
    class E:  # fake manifest entry
        name = "office"

    calls = {}
    monkeypatch.setattr(
        cli, "_load_manifest_entries", lambda config=None: ([E()], "root")
    )
    monkeypatch.setattr(cli, "_agent_dep_dir", lambda: "dep")
    monkeypatch.setattr(
        cli,
        "_wire_one",
        lambda entry, root, dep: calls.setdefault("wired", entry.name),
    )
    assert cli.wire("office") is True
    assert calls["wired"] == "office"


def test_unwire_delegates_to_wiring(monkeypatch):
    from recommended_mcp.runtime import wiring as wiring

    monkeypatch.setattr(wiring, "unwire", lambda name: name == "office")
    assert cli.unwire("office") is True
    assert cli.unwire("ghost") is False


def test_remove_delegates_to_cmd_remove(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli,
        "_cmd_remove",
        lambda name, keep: seen.update(name=name, keep=keep) or 0,
    )
    assert cli.remove("office", keep_package=True) == 0
    assert seen == {"name": "office", "keep": True}


def test_installed_state_not_installed(monkeypatch):
    class E:
        name = "office"

        class install:
            type = "source"
            package = None

    monkeypatch.setattr(
        cli, "_load_manifest_entries", lambda config=None: ([E()], "root")
    )
    monkeypatch.setattr(cli, "_agent_dep_dir", lambda: "dep")
    monkeypatch.setattr(cli, "_installed_status", lambda e, r, d: "—")
    st = cli.installed_state("office")
    assert st == {"installed": False, "version": None, "update": None}


def test_installed_state_installed_with_version(monkeypatch):
    class E:
        name = "office"
        path = "recommended_mcp/office"

        class install:
            type = "pypi"
            package = "office-mcp==0.3.1"

    monkeypatch.setattr(
        cli, "_load_manifest_entries", lambda config=None: ([E()], "root")
    )
    monkeypatch.setattr(cli, "_agent_dep_dir", lambda: "dep")
    monkeypatch.setattr(cli, "_installed_status", lambda e, r, d: "yes")
    import recommended_mcp.install as inst
    from recommended_mcp.runtime import venv as venv

    monkeypatch.setattr(inst, "_dist_name", lambda e, root: "office-mcp")
    monkeypatch.setattr(venv, "query_installed_version", lambda d: "0.3.1")
    st = cli.installed_state("office")
    assert st["installed"] is True and st["version"] == "0.3.1"


def test_installed_state_offers_update_for_outdated_wiring(monkeypatch):
    # Node агента сменилась: npm новой версии ещё нет (статус «?»), но
    # подключение отстало от рецепта — переустановка его и перепишет.
    class E:
        name = "browser_playwright"

        class install:
            type = "npx"

    monkeypatch.setattr(
        cli, "_load_manifest_entries", lambda config=None: ([E()], "root")
    )
    monkeypatch.setattr(cli, "_agent_dep_dir", lambda: "dep")
    monkeypatch.setattr(cli, "_installed_status", lambda e, r, d: "?")
    monkeypatch.setattr(cli, "_wiring_outdated", lambda e, r, d: True)
    st = cli.installed_state("browser_playwright")
    assert st == {"installed": None, "version": None, "update": "обновление"}


def _npx_playwright_on_disk(tmp_path, monkeypatch, *, npx):
    """browser_playwright подключён на диске с данным путём npx."""
    from gc_core.config_modules import env_vars
    from gc_testkit.paths import RECIPES
    from recommended_mcp.runtime import wiring

    monkeypatch.setattr(
        env_vars,
        "gc_paths",
        lambda: {
            "config": tmp_path / "cfg",
            "data": tmp_path / "d",
            "cache": tmp_path / "cache",
            "state": tmp_path / "s",
        },
    )
    wiring.wire_snippet(
        "browser_playwright",
        RECIPES / "browser_playwright",
        npx=npx,
        cache=str(tmp_path / "cache"),
    )

    class E:
        name = "browser_playwright"
        path = "browser_playwright"

        class install:
            type = "npx"

    monkeypatch.setattr(
        cli,
        "_load_manifest_entries",
        lambda config=None: ([E()], RECIPES),
    )
    monkeypatch.setattr(cli, "_agent_dep_dir", lambda: tmp_path / "d")
    monkeypatch.setattr(cli, "_installed_status", lambda e, r, d: "yes")


def test_installed_state_reads_wiring_from_disk(tmp_path, monkeypatch):
    from recommended_mcp import install as inst

    current = str(inst._node_install_paths(tmp_path / "d")["npx"])
    _npx_playwright_on_disk(tmp_path, monkeypatch, npx=current)
    assert cli.installed_state("browser_playwright")["update"] is None

    _npx_playwright_on_disk(tmp_path, monkeypatch, npx="/old/v18/npx")
    assert cli.installed_state("browser_playwright")["update"] == "обновление"


def test_package_update_wins_over_outdated_wiring(monkeypatch):
    # Пин pypi новее установленного: номер версии полезнее «обновления».
    entry = _pypi_entry("office-mcp==0.4.0")
    monkeypatch.setattr(
        cli, "_load_manifest_entries", lambda config=None: ([entry], "root")
    )
    monkeypatch.setattr(cli, "_agent_dep_dir", lambda: "dep")
    monkeypatch.setattr(cli, "_installed_status", lambda e, r, d: "yes")
    monkeypatch.setattr(cli, "_wiring_outdated", lambda e, r, d: True)
    import recommended_mcp.install as inst
    from recommended_mcp.runtime import venv as venv

    monkeypatch.setattr(inst, "_dist_name", lambda e, root: "office-mcp")
    monkeypatch.setattr(venv, "query_installed_version", lambda d: "0.3.1")
    assert cli.installed_state("x")["update"] == "0.4.0"


def test_manual_server_names_reads_runtime_key():
    server = {"transport": "stdio", "command": "x"}
    cfg = {"mcp.servers": {"corp-jira": server, "corp-confluence": server}}
    assert sorted(cli.manual_server_names(make_settings(cfg))) == [
        "corp-confluence",
        "corp-jira",
    ]
    assert cli.manual_server_names(None) == []


def _pypi_entry(package):
    class E:
        name = "x"

        class install:
            type = "pypi"

    E.install.package = package
    return E


def test_available_update_pin_newer_than_installed():
    assert (
        cli._available_update(_pypi_entry("ddgs-mcp==0.2.0"), "0.1.0")
        == "0.2.0"
    )


def test_available_update_pin_equals_installed_is_none():
    assert (
        cli._available_update(_pypi_entry("ddgs-mcp==0.2.0"), "0.2.0") is None
    )


def test_available_update_source_type_is_none():
    class E:
        class install:
            type = "source"
            package = None

    assert cli._available_update(E, "0.1.0") is None


def test_available_update_no_installed_is_none():
    assert cli._available_update(_pypi_entry("ddgs-mcp==0.2.0"), None) is None
