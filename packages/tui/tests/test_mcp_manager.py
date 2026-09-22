import pytest

pytest.importorskip("textual")

from gc_tui.services.mcp_manager import McpManager, McpRow


def test_rows_merge_installable_wired_manual(monkeypatch):
    from recommended_mcp.runtime import cli as cli
    monkeypatch.setattr(cli, "installable_names", lambda config=None: ["office", "playwright"])
    monkeypatch.setattr(cli, "wired_names", lambda config=None: ["office"])
    monkeypatch.setattr(cli, "manual_server_names", lambda uc: ["corp-jira"])
    monkeypatch.setattr(cli, "descriptions", lambda config=None: {})
    m = McpManager(config=None, user_config={"mcp-servers": {"corp-jira": {}}})
    rows = m.rows()
    assert McpRow("office", wired=True, manual=False) in rows
    assert McpRow("playwright", wired=False, manual=False) in rows
    assert McpRow("corp-jira", wired=False, manual=True) in rows
    # ручные — последними
    assert [r.name for r in rows if r.manual] == ["corp-jira"]
    assert rows[-1].manual is True


def test_rows_manual_excludes_managed_names(monkeypatch):
    """Дедуп: сервер, попавший в mcp-servers рантайм-конфига (merge config.yml +
    config.local.yml) и одновременно управляемый (manifest/wired), НЕ дублируется
    в ручной блок — там только настоящие ручные (corp-jira)."""
    from recommended_mcp.runtime import cli as cli
    monkeypatch.setattr(cli, "installable_names", lambda config=None: ["office", "duckduckgo_search"])
    monkeypatch.setattr(cli, "wired_names", lambda config=None: ["office"])
    # рантайм mcp-servers содержит и подключённый office, и ручной corp-jira
    monkeypatch.setattr(cli, "manual_server_names", lambda uc: ["office", "duckduckgo_search", "corp-jira"])
    m = McpManager(config=None, user_config={"mcp-servers": {}})
    rows = m.rows()
    manual = [r.name for r in rows if r.manual]
    assert manual == ["corp-jira"]                 # office/duckduckgo — управляемые, не дублируются
    assert sum(1 for r in rows if r.name == "office") == 1


def test_state_delegates(monkeypatch):
    from recommended_mcp.runtime import cli as cli
    monkeypatch.setattr(cli, "load_manifest_entries", lambda config=None: ([], "root"))
    monkeypatch.setattr(cli, "installed_state",
                        lambda name, config=None, *, manifest=None: {"installed": True, "version": "1.2", "update": None})
    st = McpManager().state("office")
    assert st.installed is True and st.version == "1.2"


def test_state_loads_manifest_once(monkeypatch):
    """Минор: манифест грузится один раз на фасад, а не per-строка (фоновый воркер
    зовёт state() для каждого сервера)."""
    from recommended_mcp.runtime import cli as cli
    calls = {"n": 0}
    def _load(config=None):
        calls["n"] += 1
        return ([], "root")
    monkeypatch.setattr(cli, "load_manifest_entries", _load)
    monkeypatch.setattr(cli, "installed_state",
                        lambda name, config=None, *, manifest=None: {"installed": True, "version": None, "update": None})
    m = McpManager()
    m.state("a"); m.state("b"); m.state("c")
    assert calls["n"] == 1


def test_wave_glyph_shifts():
    from gc_tui.widgets.loading_wave import wave_glyph
    assert str(wave_glyph(0)) == "···" and str(wave_glyph(0)).__len__() == 3
    assert wave_glyph(0).spans != wave_glyph(1).spans
