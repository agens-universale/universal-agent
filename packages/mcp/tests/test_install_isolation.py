import subprocess
from pathlib import Path

from recommended_mcp import install as inst
from recommended_mcp.manifest import InstallSpec, MCPEntry


def _entry(name: str) -> MCPEntry:
    return MCPEntry(
        name=name,
        path=Path("."),
        install=InstallSpec(type="pypi", package=f"{name}==1.0.0"),
    )


def test_one_failure_does_not_abort_others(monkeypatch):
    calls = []

    def fake_install_entry(entry, root, agent_dep_dir=None, **_kwargs):
        calls.append(entry.name)
        if entry.name == "bad":
            raise subprocess.CalledProcessError(1, ["pip", "install", "bad"])

    monkeypatch.setattr(inst, "install_entry", fake_install_entry)
    manifest = [_entry("good1"), _entry("bad"), _entry("good2")]
    results = inst.run_install_entries(
        manifest, Path("."), None, editable=False
    )

    assert calls == ["good1", "bad", "good2"]  # цикл не прервался
    statuses = {n: s for n, s, _ in results}
    assert statuses == {
        "good1": "installed",
        "bad": "failed",
        "good2": "installed",
    }


def test_find_entry_by_name():
    from pathlib import Path

    from recommended_mcp import install as inst
    from recommended_mcp.manifest import InstallSpec, MCPEntry

    a = MCPEntry(
        name="office", path=Path("."), install=InstallSpec(type="source")
    )
    b = MCPEntry(
        name="science", path=Path("."), install=InstallSpec(type="source")
    )
    assert inst.find_entry([a, b], "science") is b
    assert inst.find_entry([a, b], "nope") is None


def test_failure_reason_names_the_command_that_failed():
    exc = subprocess.CalledProcessError(
        1,
        [
            "/d/node/v24.22.2/bin/npx",
            "-y",
            "@playwright/mcp@0.0.70",
            "install-browser",
            "--with-deps",
            "chromium",
        ],
    )
    assert inst._short_reason(exc) == (
        "команда «npx -y @playwright/mcp@0.0.70 install-browser …» "
        "завершилась с кодом 1"
    )
    short = subprocess.CalledProcessError(2, ["/usr/bin/npm", "ls"])
    assert (
        inst._short_reason(short) == "команда «npm ls» завершилась с кодом 2"
    )


def test_platform_skip_reason_names_supported_os(monkeypatch):
    entry = MCPEntry(
        name="computer",
        path=Path("."),
        install=InstallSpec(type="source", platforms=("win32",)),
    )
    monkeypatch.setattr("sys.platform", "darwin")
    assert inst.platform_skip_reason(entry) == "только для Windows"
    results = inst.run_install_entries(
        [entry], Path("."), None, editable=False
    )
    assert results == [("computer", "skipped", "только для Windows")]
    monkeypatch.setattr("sys.platform", "win32")
    assert inst.platform_skip_reason(entry) is None
