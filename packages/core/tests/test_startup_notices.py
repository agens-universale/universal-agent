from gc_core.startup_notices import (
    Notice,
    collect_startup_notices,
    render_notices_for_cli,
)
from gc_testkit.settings import make_settings

_BUILTIN = {
    "fs_write_file",
    "fs_read_file",
    "execute_command",
    "turn_ask_human",
}


def _codes(notices):
    return [n.code for n in notices]


def test_stop_tools_unknown_flagged():
    cfg = make_settings(
        {"agent.stop_tools": ["turn_ask_human", "write_to_file"]}
    )
    ns = collect_startup_notices(cfg, _BUILTIN, None, is_tui=False)
    stop = [n for n in ns if n.code == "stop_tools_unknown"]
    assert len(stop) == 1 and stop[0].level == "warning"
    assert "write_to_file" in stop[0].message
    assert "turn_ask_human" not in stop[0].message  # валидное имя не в тексте


def test_stop_tools_all_valid_no_notice():
    cfg = make_settings(
        {"agent.stop_tools": ["turn_ask_human", "fs_write_file"]}
    )
    ns = collect_startup_notices(cfg, _BUILTIN, None, is_tui=False)
    assert "stop_tools_unknown" not in _codes(ns)


def test_stop_tools_skills_names_known_only_from_registry():
    # Без каталога сборки skills_* — не «вечно известные». С каталогом — не опечатка.
    cfg = make_settings({"agent.stop_tools": ["skills_list", "write_to_file"]})
    bare = collect_startup_notices(cfg, _BUILTIN, None, is_tui=False)
    bare_stop = [n for n in bare if n.code == "stop_tools_unknown"]
    assert len(bare_stop) == 1
    assert "skills_list" in bare_stop[0].message

    ns = collect_startup_notices(
        cfg,
        _BUILTIN,
        None,
        is_tui=False,
        known_tool_names={"skills_list", "skills_load"},
    )
    stop = [n for n in ns if n.code == "stop_tools_unknown"]
    assert len(stop) == 1
    assert "skills_list" not in stop[0].message
    assert "write_to_file" in stop[0].message


def test_stop_tools_registered_mcp_name_is_not_a_typo():
    cfg = make_settings({"agent.stop_tools": ["mcp_jira_search", "nope"]})
    ns = collect_startup_notices(
        cfg,
        _BUILTIN,
        None,
        is_tui=False,
        known_tool_names={"mcp_jira_search", "fs_read_file"},
    )
    stop = [n for n in ns if n.code == "stop_tools_unknown"]
    assert len(stop) == 1
    assert "mcp_jira_search" not in stop[0].message
    assert "nope" in stop[0].message


def test_mcp_failed_from_status():
    cfg = make_settings({})
    mcp = {"office": {"status": "failed"}, "science": {"status": "connected"}}
    ns = collect_startup_notices(cfg, _BUILTIN, mcp, is_tui=False)
    failed = [n for n in ns if n.code == "mcp_failed"]
    assert len(failed) == 1 and "office" in failed[0].message
    assert "science" not in failed[0].message


def test_deprecated_key_info():
    cfg = make_settings({})
    ns = collect_startup_notices(
        cfg,
        _BUILTIN,
        None,
        is_tui=False,
        deprecated_keys_used=["backend.stream"],
    )
    dep = [n for n in ns if n.code == "deprecated_key"]
    assert len(dep) == 1 and dep[0].level == "info"
    assert (
        "backend.stream" in dep[0].message and "tui.stream" in dep[0].message
    )


def test_agentsmd_override_ignored_when_path_and_flag():
    cfg = make_settings(
        {"paths.agent_md_path": "/p/AGENTS.md", "tui.agentsmd_override": True}
    )
    ns = collect_startup_notices(cfg, _BUILTIN, None, is_tui=True)
    assert "agentsmd_override_ignored" in _codes(ns)


def test_agentsmd_override_cli_noop_when_not_tui():
    cfg = make_settings({"tui.agentsmd_override": True})
    ns = collect_startup_notices(cfg, _BUILTIN, None, is_tui=False)
    assert "agentsmd_override_cli_noop" in _codes(ns)
    # в TUI без пути — не no-op
    ns_tui = collect_startup_notices(cfg, _BUILTIN, None, is_tui=True)
    assert "agentsmd_override_cli_noop" not in _codes(ns_tui)


def test_warnings_before_infos():
    cfg = make_settings({"agent.stop_tools": ["bogus"]})
    ns = collect_startup_notices(
        cfg,
        _BUILTIN,
        None,
        is_tui=False,
        deprecated_keys_used=["backend.stream"],
    )
    levels = [n.level for n in ns]
    assert levels == sorted(levels, key=lambda l: 0 if l == "warning" else 1)


def test_pricing_no_rates_info_when_enabled():
    notices = collect_startup_notices(
        make_settings(
            {
                "pricing": {"enabled": True, "input": 0, "output": 0},
                "agent.model": "m",
            }
        ),
        _BUILTIN,
        None,
        is_tui=False,
    )
    codes = [n.code for n in notices]
    assert "pricing_no_rates" in codes
    message = next(n.message for n in notices if n.code == "pricing_no_rates")
    assert "фактическую стоимость" in message


def test_pricing_no_rates_no_notice_when_disabled():
    cfg = make_settings(
        {"pricing": {"enabled": False, "input": 0, "output": 0}}
    )
    ns = collect_startup_notices(cfg, _BUILTIN, None, is_tui=False)
    assert "pricing_no_rates" not in _codes(ns)


def test_pricing_no_rates_no_notice_when_rates_set():
    cfg = make_settings(
        {"pricing": {"enabled": True, "input": 1.0, "output": 2.0}}
    )
    ns = collect_startup_notices(cfg, _BUILTIN, None, is_tui=False)
    assert "pricing_no_rates" not in _codes(ns)


def test_pricing_no_rates_info_without_model():
    """Если нет модели в конфиге, resolve_model_tariff вернёт None → notice."""
    cfg = make_settings({"pricing": {"enabled": True}})
    ns = collect_startup_notices(cfg, _BUILTIN, None, is_tui=False)
    assert "pricing_no_rates" in _codes(ns)


def test_cli_render_lines():
    ns = [Notice("warning", "x", "проблема A"), Notice("info", "y", "инфо B")]
    out = render_notices_for_cli(ns)
    assert "⚠ проблема A" in out and "ℹ инфо B" in out


_REGISTRY_TWO = {
    "models": {"fast": {"model": "vendor/fast"}, "big": {"model": "vendor/big"}},
    "slots": {"light": "fast", "max": "big"},
}


def test_pricing_no_notice_when_every_registry_model_has_a_rate():
    cfg = make_settings({**_REGISTRY_TWO, "pricing": {"enabled": True, "models": {
        "vendor/fast": {"input": 1, "output": 2}, "vendor/big": {"input": 3, "output": 4}}}})
    codes = _codes(collect_startup_notices(cfg, _BUILTIN, None, is_tui=False))
    assert "pricing_no_rates" not in codes and "pricing_partial_rates" not in codes


def test_pricing_partial_notice_names_the_models_without_a_rate():
    cfg = make_settings({**_REGISTRY_TWO, "pricing": {"enabled": True, "models": {
        "vendor/fast": {"input": 1, "output": 2}}}})
    notices = collect_startup_notices(cfg, _BUILTIN, None, is_tui=False)
    partial = [n for n in notices if n.code == "pricing_partial_rates"]
    assert len(partial) == 1 and "vendor/big" in partial[0].message
    assert "vendor/fast" not in partial[0].message
    assert "pricing_no_rates" not in _codes(notices)
