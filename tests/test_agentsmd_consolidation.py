"""Test that agentsmd_override conflict is surfaced via startup_notices channel."""
from startup_notices import collect_startup_notices


def test_agentsmd_conflict_surfaced_via_channel():
    """Invariant: when agentsmd_override=True and agent_md_path is set in TUI,
    the conflict is surfaced via startup notices (not invisible logger.warning)."""
    cfg = {"agent_md_path": "/p/AGENTS.md", "agentsmd_override": True}
    ns = collect_startup_notices(cfg, {"fs_write_file"}, None, is_tui=True)
    assert any(n.code == "agentsmd_override_ignored" for n in ns)
