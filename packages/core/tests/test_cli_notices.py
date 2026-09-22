from gc_core.startup_notices import collect_startup_notices, render_notices_for_cli
from gc_core.tools.registry import all_builtin_tool_names


def test_cli_pipeline_flags_stop_tools_typo():
    cfg = {"stop_tools": ["write_to_file"]}
    notices = collect_startup_notices(
        cfg,
        all_builtin_tool_names(cfg),
        None,
        is_tui=False,
        deprecated_keys_used=cfg.get("deprecated_keys_used"),
    )
    out = render_notices_for_cli(notices)
    assert "write_to_file" in out and "⚠" in out
