import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config_modules import env_vars
from project_memory.compaction import CompactionResult
from tools.project.project_memory import ProjectMemory
from tools.registry import get_builtin_tools_data


@pytest.fixture(autouse=True)
def _isolate_project_memory_gc_home(tmp_path, monkeypatch):
    """Не писать backup в реальный gc_state — иначе re-run с тем же basetemp
    восстанавливает leftover backup и ломает append/read assertions (length=22)."""
    monkeypatch.setattr(env_vars, "env", env_vars.EnvVars(gc_home=tmp_path / "gc_home"))


def _make_tool(tmp_path) -> ProjectMemory:
    ctx = SimpleNamespace(
        project_gc_dir=tmp_path / ".gc",
        config_dict={"backend_api_mode": "chat_completions", "model": "test-model"},
        auth_connector=None,
        logger=None,
    )
    state = SimpleNamespace()
    return ProjectMemory(ctx=ctx, state=state)


def test_tool_description_has_three_actions_only():
    desc = ProjectMemory.tool_description()
    enum = desc["parameters"]["properties"]["action"]["enum"]
    assert sorted(enum) == sorted(["read", "append", "write"])
    assert "compact" not in enum


def test_tool_description_requires_action_and_content():
    required = ProjectMemory.tool_description()["parameters"]["required"]
    assert required == ["action", "content"]
    content_desc = ProjectMemory.tool_description()["parameters"]["properties"]["content"][
        "description"
    ].lower()
    assert "required for every action" in content_desc
    assert "ignored for read" in content_desc


def test_parse_and_validate_rejects_append_without_content():
    from tools.arguments_validation import (
        WrongFunctionArgumentsException,
        parse_and_validate_tool_arguments,
    )

    spec = ProjectMemory.tool_description()
    with pytest.raises(WrongFunctionArgumentsException):
        parse_and_validate_tool_arguments(
            tool_name="project_memory",
            arguments=json.dumps({"action": "append"}),
            tool_spec=spec,
            allow_empty_for_empty_schema=False,
            tool_fabric=ProjectMemory,
        )


def test_tool_description_does_not_leak_path_or_compaction():
    desc = ProjectMemory.tool_description()
    description_text = desc["description"]
    assert ".gc" not in description_text
    assert "project_memory.md" not in description_text
    assert "Compact" not in description_text  # compact action removed
    assert "compaction" not in description_text.lower()


def test_prompt_description_does_not_leak_path():
    text = ProjectMemory.prompt_description()
    assert ".gc" not in text
    assert "project_memory.md" not in text


def test_tool_read_missing_file_returns_ok_with_empty_content(tmp_path):
    tool = _make_tool(tmp_path)
    res = json.loads(tool(action="read"))
    assert res["ok"] is True
    assert res["action"] == "read"
    assert res["content"] == ""
    assert res["length"] == 0
    assert res["lines"] == 0
    assert res["max_chars"] == 10_000
    assert res["fill_percent"] == 0
    assert res["max_append_chars"] == 3_000
    assert "hint" in res


def test_tool_read_with_empty_content_arg_ok(tmp_path):
    """Schema requires content; read ignores it — empty string is fine."""
    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "project_memory.md").write_text("kept", encoding="utf-8")
    tool = _make_tool(tmp_path)
    res = json.loads(tool(action="read", content=""))
    assert res["ok"] is True
    assert res["content"] == "kept"


def test_tool_read_existing_file_returns_full_content(tmp_path):
    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "project_memory.md").write_text("hello world", encoding="utf-8")
    tool = _make_tool(tmp_path)
    res = json.loads(tool(action="read"))
    assert res["ok"] is True
    assert res["content"] == "hello world"
    assert res["length"] == len("hello world")
    assert res["lines"] == 1
    assert res["max_chars"] == 10_000
    assert res["fill_percent"] == round(len("hello world") * 100 / 10_000)


def test_tool_append_with_empty_content_fails(tmp_path):
    tool = _make_tool(tmp_path)
    res = json.loads(tool(action="append", content="   "))
    assert res["ok"] is False
    assert res["error"]["type"] == "empty_content"


def test_tool_append_over_soft_limit_refuses(tmp_path):
    tool = _make_tool(tmp_path)
    huge_delta = "x" * 3_001
    res = json.loads(tool(action="append", content=huge_delta))
    assert res["ok"] is False
    assert res["error"]["type"] == "input_too_long"
    assert "3000" in res["error"]["message"]
    assert res["max_chars"] == 10_000
    assert res["fill_percent"] == 0
    # File must not be created.
    assert not (tmp_path / ".gc" / "project_memory.md").exists()


def test_tool_append_fast_path_no_compaction(tmp_path):
    tool = _make_tool(tmp_path)
    res = json.loads(tool(action="append", content="first fact"))
    assert res["ok"] is True
    assert res["compaction"] == {"performed": False}
    assert res["length"] == len("first fact")
    assert (tmp_path / ".gc" / "project_memory.md").read_text(encoding="utf-8") == "first fact"


def test_tool_append_persists_across_instances(tmp_path):
    t1 = _make_tool(tmp_path)
    t1(action="append", content="from t1")
    t2 = _make_tool(tmp_path)
    res = json.loads(t2(action="read"))
    assert "from t1" in res["content"]


def _seed_memory(tmp_path, content: str) -> None:
    (tmp_path / ".gc").mkdir(exist_ok=True)
    (tmp_path / ".gc" / "project_memory.md").write_text(content, encoding="utf-8")


def test_tool_append_overflow_triggers_compaction(tmp_path):
    """existing+delta > max_chars → compact_text called with combined text → file = compacted."""
    _seed_memory(tmp_path, "x" * 9_900)
    tool = _make_tool(tmp_path)
    delta = "y" * 200  # combined > 10_000

    captured = {}

    def fake_compact(text, ctx, state=None):
        captured["input"] = text
        return CompactionResult(ok=True, text="COMPACTED", error_type=None, error_message=None)

    with patch("project_memory.compaction.compact_text", side_effect=fake_compact):
        res = json.loads(tool(action="append", content=delta))

    # Combined text fed to compaction was existing + \n\n + delta, both stripped.
    assert "x" * 9_900 in captured["input"]
    assert "y" * 200 in captured["input"]

    assert res["ok"] is True
    assert res["compaction"]["performed"] is True
    assert res["compaction"]["before_chars"] > 10_000
    assert res["compaction"]["after_chars"] == len("COMPACTED")
    assert "Memory was compacted" in res["hint"]
    assert "action=read" in res["hint"]
    assert (tmp_path / ".gc" / "project_memory.md").read_text(encoding="utf-8") == "COMPACTED"


def test_tool_append_overflow_compaction_failed(tmp_path):
    _seed_memory(tmp_path, "x" * 9_900)
    tool = _make_tool(tmp_path)

    failed = CompactionResult(
        ok=False, text="", error_type="backend_error", error_message="boom"
    )
    with patch("project_memory.compaction.compact_text", return_value=failed):
        res = json.loads(tool(action="append", content="y" * 200))

    assert res["ok"] is False
    assert res["error"]["type"] == "compaction_failed"
    assert "boom" in res["error"]["message"]
    # File untouched.
    assert (tmp_path / ".gc" / "project_memory.md").read_text(encoding="utf-8") == "x" * 9_900


def test_tool_append_overflow_compaction_result_still_too_large(tmp_path):
    _seed_memory(tmp_path, "x" * 9_900)
    tool = _make_tool(tmp_path)

    over = CompactionResult(
        ok=True, text="z" * 10_001, error_type=None, error_message=None
    )
    with patch("project_memory.compaction.compact_text", return_value=over):
        res = json.loads(tool(action="append", content="y" * 200))

    assert res["ok"] is False
    assert res["error"]["type"] == "limit_exceeded"
    assert (tmp_path / ".gc" / "project_memory.md").read_text(encoding="utf-8") == "x" * 9_900


def test_tool_append_fast_path_does_not_call_compaction(tmp_path):
    tool = _make_tool(tmp_path)
    with patch("project_memory.compaction.compact_text") as mocked:
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
    assert (tmp_path / ".gc" / "project_memory.md").read_text(encoding="utf-8") == "full memory body"


def test_tool_write_over_max_chars_refuses_no_compaction(tmp_path):
    tool = _make_tool(tmp_path)
    huge = "x" * (10_001)

    with patch("project_memory.compaction.compact_text") as mocked:
        res = json.loads(tool(action="write", content=huge))

    assert res["ok"] is False
    assert res["error"]["type"] == "input_too_long"
    assert "10000" in res["error"]["message"]
    # write never invokes compaction.
    assert not mocked.called
    assert not (tmp_path / ".gc" / "project_memory.md").exists()


def test_tool_write_overwrites_existing(tmp_path):
    _seed_memory(tmp_path, "old body")
    tool = _make_tool(tmp_path)
    json.loads(tool(action="write", content="new body"))
    assert (tmp_path / ".gc" / "project_memory.md").read_text(encoding="utf-8") == "new body"


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


def test_project_memory_registered_by_default():
    data = get_builtin_tools_data({})
    assert "project_memory" in data
    entry = data["project_memory"]
    assert entry["spec"]["name"] == "project_memory"
    assert callable(entry["tool_fabric"])


def test_project_memory_filtered_by_stop_tools():
    data = get_builtin_tools_data({"stop_tools": ["project_memory"]})
    assert "project_memory" not in data


def _make_tool_with_limits(tmp_path, *, max_chars: int) -> ProjectMemory:
    ctx = SimpleNamespace(
        project_gc_dir=tmp_path / ".gc",
        config_dict={
            "backend_api_mode": "chat_completions",
            "project_memory_params": {"max_chars": max_chars},
        },
        auth_connector=None,
        logger=None,
    )
    return ProjectMemory(ctx=ctx, state=SimpleNamespace())


def test_tool_read_high_fill_includes_reorganize_hint(tmp_path):
    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "project_memory.md").write_text("x" * 71, encoding="utf-8")
    tool = _make_tool_with_limits(tmp_path, max_chars=100)
    res = json.loads(tool(action="read"))
    assert res["fill_percent"] == 71
    assert "reorganize" in res["hint"].lower()
    assert "action=write" in res["hint"]
    assert "artifact" in res["hint"].lower()


def test_tool_read_at_70_percent_no_reorganize_hint(tmp_path):
    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "project_memory.md").write_text("x" * 70, encoding="utf-8")
    tool = _make_tool_with_limits(tmp_path, max_chars=100)
    res = json.loads(tool(action="read"))
    assert res["fill_percent"] == 70
    assert "hint" not in res


def test_tool_append_high_fill_includes_reorganize_hint(tmp_path):
    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "project_memory.md").write_text("x" * 70, encoding="utf-8")
    tool = _make_tool_with_limits(tmp_path, max_chars=100)
    res = json.loads(tool(action="append", content="y"))
    assert res["fill_percent"] > 70
    assert "reorganize" in res["hint"].lower()


def test_tool_uses_custom_limits_from_config(tmp_path):
    ctx = SimpleNamespace(
        project_gc_dir=tmp_path / ".gc",
        config_dict={
            "backend_api_mode": "chat_completions",
            "project_memory_params": {
                "max_chars": 100,
                "max_append_chars": 40,
            },
        },
        auth_connector=None,
        logger=None,
    )
    tool = ProjectMemory(ctx=ctx, state=SimpleNamespace())

    res = json.loads(tool(action="append", content="x" * 41))
    assert res["ok"] is False
    assert res["error"]["type"] == "input_too_long"
    assert "40" in res["error"]["message"]

    res = json.loads(tool(action="write", content="y" * 101))
    assert res["ok"] is False
    assert res["error"]["type"] == "input_too_long"
    assert "100" in res["error"]["message"]
