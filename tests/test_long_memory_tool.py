import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from long_memory.compaction import CompactionResult
from tools.session.long_memory import LongMemory
from tools.registry import get_builtin_tools_data


def _make_tool(tmp_path) -> LongMemory:
    ctx = SimpleNamespace(
        project_gc_dir=tmp_path / ".gc",
        config_dict={"backend_api_mode": "chat_completions", "model": "test-model"},
        auth_connector=None,
        logger=None,
    )
    state = SimpleNamespace()
    return LongMemory(ctx=ctx, state=state)


def test_tool_description_has_three_actions_only():
    desc = LongMemory.tool_description()
    enum = desc["parameters"]["properties"]["action"]["enum"]
    assert sorted(enum) == sorted(["read", "append", "write"])
    assert "compact" not in enum


def test_tool_description_does_not_leak_path_or_compaction():
    desc = LongMemory.tool_description()
    description_text = desc["description"]
    assert ".gc" not in description_text
    assert "long_memory.md" not in description_text
    assert "Compact" not in description_text  # compact action removed
    assert "compaction" not in description_text.lower()


def test_prompt_description_does_not_leak_path():
    text = LongMemory.prompt_description()
    assert ".gc" not in text
    assert "long_memory.md" not in text


def test_tool_read_missing_file_returns_ok_with_empty_content(tmp_path):
    tool = _make_tool(tmp_path)
    res = json.loads(tool(action="read"))
    assert res["ok"] is True
    assert res["action"] == "read"
    assert res["content"] == ""
    assert res["length"] == 0
    assert res["lines"] == 0
    assert "hint" in res


def test_tool_read_existing_file_returns_full_content(tmp_path):
    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "long_memory.md").write_text("hello world", encoding="utf-8")
    tool = _make_tool(tmp_path)
    res = json.loads(tool(action="read"))
    assert res["ok"] is True
    assert res["content"] == "hello world"
    assert res["length"] == len("hello world")
    assert res["lines"] == 1


def test_tool_append_with_empty_content_fails(tmp_path):
    tool = _make_tool(tmp_path)
    res = json.loads(tool(action="append", content="   "))
    assert res["ok"] is False
    assert res["error"]["type"] == "empty_content"


def test_tool_append_over_soft_limit_refuses(tmp_path):
    tool = _make_tool(tmp_path)
    huge_delta = "x" * 20_001
    res = json.loads(tool(action="append", content=huge_delta))
    assert res["ok"] is False
    assert res["error"]["type"] == "input_too_long"
    assert "20000" in res["error"]["message"]
    # File must not be created.
    assert not (tmp_path / ".gc" / "long_memory.md").exists()


def test_tool_append_fast_path_no_compaction(tmp_path):
    tool = _make_tool(tmp_path)
    res = json.loads(tool(action="append", content="first fact"))
    assert res["ok"] is True
    assert res["compaction"] == {"performed": False}
    assert res["length"] == len("first fact")
    assert (tmp_path / ".gc" / "long_memory.md").read_text(encoding="utf-8") == "first fact"


def test_tool_append_persists_across_instances(tmp_path):
    t1 = _make_tool(tmp_path)
    t1(action="append", content="from t1")
    t2 = _make_tool(tmp_path)
    res = json.loads(t2(action="read"))
    assert "from t1" in res["content"]


def _seed_memory(tmp_path, content: str) -> None:
    (tmp_path / ".gc").mkdir(exist_ok=True)
    (tmp_path / ".gc" / "long_memory.md").write_text(content, encoding="utf-8")


def test_tool_append_overflow_triggers_compaction(tmp_path):
    """existing+delta > 50k → compact_text called with combined text → file = compacted."""
    _seed_memory(tmp_path, "x" * 49_900)
    tool = _make_tool(tmp_path)
    delta = "y" * 200  # combined > 50_000

    captured = {}

    def fake_compact(text, ctx, state=None):
        captured["input"] = text
        return CompactionResult(ok=True, text="COMPACTED", error_type=None, error_message=None)

    with patch("long_memory.compaction.compact_text", side_effect=fake_compact):
        res = json.loads(tool(action="append", content=delta))

    # Combined text fed to compaction was existing + \n\n + delta, both stripped.
    assert "x" * 49_900 in captured["input"]
    assert "y" * 200 in captured["input"]

    assert res["ok"] is True
    assert res["compaction"]["performed"] is True
    assert res["compaction"]["before_chars"] > 50_000
    assert res["compaction"]["after_chars"] == len("COMPACTED")
    assert "Memory was compacted" in res["hint"]
    assert "action=read" in res["hint"]
    assert (tmp_path / ".gc" / "long_memory.md").read_text(encoding="utf-8") == "COMPACTED"


def test_tool_append_overflow_compaction_failed(tmp_path):
    _seed_memory(tmp_path, "x" * 49_900)
    tool = _make_tool(tmp_path)

    failed = CompactionResult(
        ok=False, text="", error_type="backend_error", error_message="boom"
    )
    with patch("long_memory.compaction.compact_text", return_value=failed):
        res = json.loads(tool(action="append", content="y" * 200))

    assert res["ok"] is False
    assert res["error"]["type"] == "compaction_failed"
    assert "boom" in res["error"]["message"]
    # File untouched.
    assert (tmp_path / ".gc" / "long_memory.md").read_text(encoding="utf-8") == "x" * 49_900


def test_tool_append_overflow_compaction_result_still_too_large(tmp_path):
    _seed_memory(tmp_path, "x" * 49_900)
    tool = _make_tool(tmp_path)

    over = CompactionResult(
        ok=True, text="z" * 50_001, error_type=None, error_message=None
    )
    with patch("long_memory.compaction.compact_text", return_value=over):
        res = json.loads(tool(action="append", content="y" * 200))

    assert res["ok"] is False
    assert res["error"]["type"] == "limit_exceeded"
    assert (tmp_path / ".gc" / "long_memory.md").read_text(encoding="utf-8") == "x" * 49_900


def test_tool_append_fast_path_does_not_call_compaction(tmp_path):
    tool = _make_tool(tmp_path)
    with patch("long_memory.compaction.compact_text") as mocked:
        json.loads(tool(action="append", content="small"))
    assert not mocked.called


def test_tool_write_with_empty_content_fails(tmp_path):
    tool = _make_tool(tmp_path)
    res = json.loads(tool(action="write", content="  \n  "))
    assert res["ok"] is False
    assert res["error"]["type"] == "empty_content"


def test_tool_write_under_max_chars_succeeds(tmp_path):
    tool = _make_tool(tmp_path)
    res = json.loads(tool(action="write", content="full memory body"))
    assert res["ok"] is True
    assert res["compaction"] == {"performed": False}
    assert (tmp_path / ".gc" / "long_memory.md").read_text(encoding="utf-8") == "full memory body"


def test_tool_write_over_max_chars_refuses_no_compaction(tmp_path):
    tool = _make_tool(tmp_path)
    huge = "x" * (50_001)

    with patch("long_memory.compaction.compact_text") as mocked:
        res = json.loads(tool(action="write", content=huge))

    assert res["ok"] is False
    assert res["error"]["type"] == "input_too_long"
    assert "50000" in res["error"]["message"]
    # write never invokes compaction.
    assert not mocked.called
    assert not (tmp_path / ".gc" / "long_memory.md").exists()


def test_tool_write_overwrites_existing(tmp_path):
    _seed_memory(tmp_path, "old body")
    tool = _make_tool(tmp_path)
    json.loads(tool(action="write", content="new body"))
    assert (tmp_path / ".gc" / "long_memory.md").read_text(encoding="utf-8") == "new body"


def test_tool_invalid_action_compact(tmp_path):
    """action=compact is intentionally NOT exposed; should return invalid_action."""
    tool = _make_tool(tmp_path)
    res = json.loads(tool(action="compact"))
    assert res["ok"] is False
    assert res["error"]["type"] == "invalid_action"


def test_tool_unknown_action(tmp_path):
    tool = _make_tool(tmp_path)
    res = json.loads(tool(action="purge"))
    assert res["ok"] is False
    assert res["error"]["type"] == "invalid_action"


def test_long_memory_registered_by_default():
    data = get_builtin_tools_data({})
    assert "long_memory" in data
    entry = data["long_memory"]
    assert entry["spec"]["name"] == "long_memory"
    assert callable(entry["tool_fabric"])


def test_long_memory_filtered_by_stop_tools():
    data = get_builtin_tools_data({"stop_tools": ["long_memory"]})
    assert "long_memory" not in data
