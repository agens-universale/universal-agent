"""Tests for public API wrappers in mcp_runtime.cli."""
from recommended_mcp.runtime import cli as mcp


def test_installable_names_from_bundled_manifest():
    """Check that installable_names() returns list of names from bundled manifest."""
    names = mcp.installable_names()
    assert isinstance(names, list)
    assert "office" in names        # из recommended_mcp/config.yml


def test_install_delegates(monkeypatch):
    """Check that install() delegates to _cmd_install with correct arguments."""
    called = {}

    def mock_install(name, config=None):
        called["name"] = name
        called["config"] = config
        return 0

    monkeypatch.setattr(mcp, "_cmd_install", mock_install)
    assert mcp.install("office") == 0
    assert called["name"] == "office"
    assert called["config"] is None


def test_help_command_lists_all_subcommands(capsys):
    """gc-mcp help печатает справку со всеми командами."""
    assert mcp.main(["help"]) == 0
    out = capsys.readouterr().out
    assert "install" in out
    assert "list" in out
    assert "remove" in out


def test_no_command_shows_help(capsys):
    """gc-mcp без аргумента → справка (не argparse-ошибка), код 0."""
    assert mcp.main([]) == 0
    assert "gc-mcp" in capsys.readouterr().out
