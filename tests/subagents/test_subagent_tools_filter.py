"""Фильтр набора тулов дочернего рана (allow/stop из определения сабагента)."""
import logging

import pytest

from tools import (
    SUBAGENT_TOOLS_ALLOW_KEY,
    SUBAGENT_TOOLS_STOP_KEY,
    apply_subagent_tools_filter,
)

REQUIRED = ("turn_finish", "session_read_stored_tool_output")


def _tools_data(*names):
    return {name: {"spec": {"name": name}, "description": name} for name in names}


BASE = _tools_data(
    "fs_read_file", "fs_write_file", "exec_command", "exec_python",
    "mcp_jira_search_issues", "mcp_jira_create_issue", *REQUIRED,
)


class TestAbsence:
    def test_no_keys_changes_nothing(self):
        # Регрессия: главный агент не должен пострадать от появления фильтра.
        assert apply_subagent_tools_filter(dict(BASE), {}) == BASE

    def test_explicit_none_allow_and_empty_stop_change_nothing(self):
        cfg = {SUBAGENT_TOOLS_ALLOW_KEY: None, SUBAGENT_TOOLS_STOP_KEY: ()}
        assert apply_subagent_tools_filter(dict(BASE), cfg) == BASE


class TestAllowlist:
    def test_keeps_only_listed_plus_core(self):
        cfg = {SUBAGENT_TOOLS_ALLOW_KEY: ("fs_read_file", *REQUIRED)}
        out = apply_subagent_tools_filter(dict(BASE), cfg)
        assert set(out) == {"fs_read_file", *REQUIRED}

    def test_mcp_tool_can_be_allowed(self):
        cfg = {SUBAGENT_TOOLS_ALLOW_KEY: ("mcp_jira_search_issues", *REQUIRED)}
        out = apply_subagent_tools_filter(dict(BASE), cfg)
        assert "mcp_jira_search_issues" in out
        assert "mcp_jira_create_issue" not in out


class TestStopList:
    def test_subtracts(self):
        cfg = {SUBAGENT_TOOLS_STOP_KEY: ("exec_python",)}
        out = apply_subagent_tools_filter(dict(BASE), cfg)
        assert "exec_python" not in out
        assert "exec_command" in out

    def test_cuts_mcp_unlike_global_stop_tools(self):
        # Глобальный agent.stop_tools применяется внутри get_builtin_tools_data и
        # до MCP не дотягивается; этот фильтр обязан дотягиваться.
        cfg = {SUBAGENT_TOOLS_STOP_KEY: ("mcp_jira_create_issue",)}
        out = apply_subagent_tools_filter(dict(BASE), cfg)
        assert "mcp_jira_create_issue" not in out
        assert "mcp_jira_search_issues" in out

    def test_stop_wins_over_allow(self):
        cfg = {
            SUBAGENT_TOOLS_ALLOW_KEY: ("fs_read_file", "exec_command", *REQUIRED),
            SUBAGENT_TOOLS_STOP_KEY: ("exec_command",),
        }
        out = apply_subagent_tools_filter(dict(BASE), cfg)
        assert set(out) == {"fs_read_file", *REQUIRED}


class TestCore:
    def test_stop_list_cannot_take_core_away(self):
        # Ядро — гарантия, а не предпочтение: stop_tools его не отбирает.
        cfg = {SUBAGENT_TOOLS_STOP_KEY: REQUIRED}
        out = apply_subagent_tools_filter(dict(BASE), cfg)
        for name in REQUIRED:
            assert name in out

    def test_core_arrives_without_being_listed(self):
        # Перечислять ядро в tools не требуется — оно доливается само.
        cfg = {SUBAGENT_TOOLS_ALLOW_KEY: ("fs_read_file",)}
        out = apply_subagent_tools_filter(dict(BASE), cfg)
        assert set(out) == {"fs_read_file", *REQUIRED}

    def test_absent_core_is_not_invented(self):
        # Если ядра нет в исходном наборе (глобальный stop_tools, harbor) —
        # восстанавливать нечего.
        data = _tools_data("fs_read_file")
        cfg = {SUBAGENT_TOOLS_ALLOW_KEY: ("fs_read_file",)}
        assert set(apply_subagent_tools_filter(data, cfg)) == {"fs_read_file"}


class TestWarnings:
    def test_unknown_name_warns_and_does_not_raise(self, caplog):
        cfg = {SUBAGENT_TOOLS_ALLOW_KEY: ("fs_read_file", "fs_read_files", *REQUIRED)}
        with caplog.at_level(logging.WARNING):
            out = apply_subagent_tools_filter(dict(BASE), cfg)
        assert "fs_read_file" in out
        assert any("subagent_tools_filter_unknown" in r.getMessage() for r in caplog.records)

    def test_nested_subagent_names_get_their_own_warning(self, caplog):
        # Глубина всегда 1 — сабагенты ребёнку не выдаются в принципе.
        cfg = {SUBAGENT_TOOLS_ALLOW_KEY: ("fs_read_file", "subagent_worker", *REQUIRED)}
        with caplog.at_level(logging.WARNING):
            apply_subagent_tools_filter(dict(BASE), cfg)
        msgs = [r.getMessage() for r in caplog.records]
        assert any("subagent_tools_filter_nested_ignored" in m for m in msgs)


class TestReachesSpec:
    def test_filtered_set_reaches_tools_spec(self, tmp_path):
        """Модель не должна видеть в схеме тул, который не сможет вызвать."""
        from tools import build_tools_data

        cfg = {
            "launch_root": str(tmp_path),
            "backend_api_mode": "chat_completions",
            SUBAGENT_TOOLS_ALLOW_KEY: ("fs_read_file", *REQUIRED),
        }
        _data, spec, _descr, _mcp = build_tools_data(cfg)
        spec_names = {s.get("name") for s in spec}
        assert "fs_read_file" in spec_names
        assert "exec_command" not in spec_names
