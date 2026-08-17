"""tool_identity: wire-имя тула → канонический id (старое голое имя)."""
from gc_tui.services.tool_identity import tool_id


def test_control_tools_new_to_legacy():
    assert tool_id("turn_ask_human") == "ask_human"
    assert tool_id("turn_finish") == "submit_solution"
    assert tool_id("turn_intermediate_reply") == "intermediate_reply"


def test_fs_tools_new_to_legacy():
    assert tool_id("fs_read_file") == "read_file"
    assert tool_id("fs_write_file") == "write_to_file"
    assert tool_id("fs_append_file") == "append_to_file"
    assert tool_id("fs_list_files") == "list_files"
    assert tool_id("fs_search_files") == "search_files"
    assert tool_id("fs_search_and_replace") == "search_and_replace"


def test_exec_tools_new_to_legacy():
    assert tool_id("exec_command") == "execute_command"
    assert tool_id("exec_python") == "run_python_code"
    assert tool_id("exec_fetch_url") == "fetch_url"


def test_legacy_names_pass_through():
    assert tool_id("ask_human") == "ask_human"
    assert tool_id("submit_solution") == "submit_solution"
    assert tool_id("read_file") == "read_file"


def test_unknown_and_empty_pass_through():
    assert tool_id("session_memory") == "session_memory"   # не маппим (generic-рендер)
    assert tool_id("some_future_tool") == "some_future_tool"
    assert tool_id("") == ""
    assert tool_id(None) is None
