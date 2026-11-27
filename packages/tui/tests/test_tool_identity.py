"""tool_identity: wire-имя тула → канонический id (старое голое имя)."""

import json

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
    assert tool_id("fs_find_files_by_name") == "find_files_by_name"
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
    assert (
        tool_id("session_memory") == "session_memory"
    )  # не маппим (generic-рендер)
    assert tool_id("some_future_tool") == "some_future_tool"
    assert tool_id("") == ""
    assert tool_id(None) is None


def test_find_in_files_reaches_the_search_summary_branch():
    """Без маппинга строка трейса теряла pattern и печатала сырое wire-имя.

    Канонический id — тот же, что у старого имени `fs_search_files`: старые
    и новые треды маппятся одинаково.
    """
    assert tool_id("fs_find_in_files") == "search_files"


def test_find_in_files_summary_keeps_the_pattern():
    from gc_tui.services.trace_mapper import map_message

    ev = map_message(
        {
            "type": "function_call",
            "name": "fs_find_in_files",
            "arguments": json.dumps(
                {
                    "query_mode": "text",
                    "file_pattern": "*.py",
                    "path": "/proj",
                    "pattern": "def abuild_tools_data",
                }
            ),
        },
        no=1,
        ts="t",
    )
    assert "def abuild_tools_data" in ev.summary


def test_finish_is_recognized_by_one_predicate():
    """«Это сдача хода» — одно правило (`trace_mapper.is_finish`,
    `FINISH_ID`): строка-литерал канонического id сдачи в коде — только у
    владельцев имени (`tool_identity`, маппер), ни в сравнении, ни в
    наборе, ни в ``in (…)``. Старое имя `submit_solution` id не меняет —
    старые треды узнаются тем же правилом."""
    import ast
    import io
    import tokenize

    from gc_testkit.paths import package_dir
    from gc_tui.services.trace_mapper import FINISH_ID

    assert tool_id("submit_solution") == FINISH_ID
    owners = {"tool_identity.py", "trace_mapper.py"}
    copies = []
    for path in package_dir("gc_tui").rglob("*.py"):
        if path.name in owners:
            continue
        source = io.StringIO(path.read_text()).readline
        for tok in tokenize.generate_tokens(source):
            if tok.type != tokenize.STRING:
                continue
            try:
                value = ast.literal_eval(tok.string)
            except ValueError:
                continue
            if value == FINISH_ID:
                copies.append(f"{path.name}:{tok.start[0]}")
    assert copies == []
