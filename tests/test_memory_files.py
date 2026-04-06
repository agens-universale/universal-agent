"""Tests for tools.simple_tools.memory_files.MemoryFiles"""

import json
from types import SimpleNamespace

import pytest

from tools.simple_tools.memory_files import (
    MemoryFiles,
    _DEFAULT_MAX_FILE_CHARS,
    _DEFAULT_MAX_FILES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(memory_files=None):
    """Minimal state stub with memory_files dict."""
    state = SimpleNamespace()
    state.memory_files = memory_files if memory_files is not None else {}
    return state


def _parse(raw: str):
    return json.loads(raw)


def _call(state, **kwargs):
    tool = MemoryFiles(parent_state=state)
    return _parse(tool(**kwargs))


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_empty(self):
        res = _call(_make_state(), action="list")
        assert res["ok"] is True
        assert res["action"] == "list"
        assert res["files"] == {}
        assert res["total_chars"] == 0
        assert "hint" in res
        assert "append" in res["hint"].lower() or "write" in res["hint"].lower()

    def test_with_files(self):
        state = _make_state({"journal": "abc", "arch": "x" * 100})
        res = _call(state, action="list")
        assert res["ok"] is True
        assert res["files"] == {"journal": 3, "arch": 100}
        assert res["total_chars"] == 103
        assert "hint" not in res


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


class TestRead:
    def test_journal_empty(self):
        res = _call(_make_state(), action="read")
        assert res["ok"] is True
        assert "empty" in res["content"].lower()

    def test_journal_with_content(self):
        state = _make_state({"journal": "hello world"})
        res = _call(state, action="read", file_name="journal")
        assert res["ok"] is True
        assert res["content"] == "hello world"

    def test_nonexistent_file(self):
        res = _call(_make_state(), action="read", file_name="missing")
        assert res["ok"] is False
        assert res["error"]["type"] == "not_found"

    def test_invalid_name(self):
        res = _call(_make_state(), action="read", file_name="no spaces!")
        assert res["ok"] is False
        assert res["error"]["type"] == "invalid_name"


# ---------------------------------------------------------------------------
# append
# ---------------------------------------------------------------------------


class TestAppend:
    def test_append_to_empty_journal(self):
        state = _make_state()
        res = _call(state, action="append", content="first entry")
        assert res["ok"] is True
        assert state.memory_files["journal"] == "first entry"

    def test_append_to_existing(self):
        state = _make_state({"journal": "line1"})
        res = _call(state, action="append", content="line2")
        assert res["ok"] is True
        assert state.memory_files["journal"] == "line1\n\nline2"

    def test_append_creates_new_file(self):
        state = _make_state()
        res = _call(state, action="append", file_name="arch", content="decision A")
        assert res["ok"] is True
        assert state.memory_files["arch"] == "decision A"

    def test_append_empty_content_rejected(self):
        res = _call(_make_state(), action="append", content="   ")
        assert res["ok"] is False
        assert res["error"]["type"] == "empty_content"

    def test_append_invalid_name(self):
        res = _call(_make_state(), action="append", file_name="123bad", content="x")
        assert res["ok"] is False
        assert res["error"]["type"] == "invalid_name"


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class TestWrite:
    def test_write_new_file(self):
        state = _make_state()
        res = _call(state, action="write", file_name="arch", content="v1")
        assert res["ok"] is True
        assert state.memory_files["arch"] == "v1"

    def test_write_overwrite(self):
        state = _make_state({"arch": "old"})
        res = _call(state, action="write", file_name="arch", content="new")
        assert res["ok"] is True
        assert state.memory_files["arch"] == "new"

    def test_write_empty_rejected(self):
        res = _call(_make_state(), action="write", file_name="arch", content="")
        assert res["ok"] is False
        assert res["error"]["type"] == "empty_content"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_file(self):
        state = _make_state({"journal": "j", "arch": "a"})
        res = _call(state, action="delete", file_name="arch")
        assert res["ok"] is True
        assert "arch" not in state.memory_files

    def test_delete_journal_protected(self):
        state = _make_state({"journal": "j"})
        res = _call(state, action="delete", file_name="journal")
        assert res["ok"] is False
        assert res["error"]["type"] == "protected"

    def test_delete_nonexistent(self):
        res = _call(_make_state(), action="delete", file_name="nope")
        assert res["ok"] is False
        assert res["error"]["type"] == "not_found"


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


class TestLimits:
    def test_file_char_limit(self):
        state = _make_state()
        huge = "x" * (_DEFAULT_MAX_FILE_CHARS + 1)
        res = _call(state, action="write", file_name="big", content=huge)
        assert res["ok"] is False
        assert res["error"]["type"] == "file_limit_exceeded"
        assert "big" not in state.memory_files

    def test_total_budget_exceeded(self):
        budget = 1000
        files = {"journal": "a" * 600, "arch": "b" * 300}
        state = _make_state(files)
        tool = MemoryFiles(parent_state=state)
        tool.TOTAL_BUDGET_CHARS = budget
        res = _parse(tool(action="write", file_name="extra", content="c" * 200))
        assert res["ok"] is False
        assert res["error"]["type"] == "total_budget_exceeded"

    def test_max_files_limit(self):
        files = {f"f{i}": "x" for i in range(_DEFAULT_MAX_FILES)}
        state = _make_state(files)
        res = _call(state, action="write", file_name="overflow", content="data")
        assert res["ok"] is False
        assert res["error"]["type"] == "max_files_exceeded"

    def test_budget_from_config(self):
        ctx = SimpleNamespace(config_dict={"memory_files_total_budget_chars": 500})
        state = _make_state({"journal": "a" * 400})
        tool = MemoryFiles(ctx=ctx, parent_state=state)
        res = _parse(tool(action="write", file_name="extra", content="b" * 200))
        assert res["ok"] is False
        assert res["error"]["type"] == "total_budget_exceeded"

    def test_max_file_chars_from_config(self):
        ctx = SimpleNamespace(config_dict={"memory_files_max_file_chars": 100})
        state = _make_state()
        tool = MemoryFiles(ctx=ctx, parent_state=state)
        res = _parse(tool(action="write", file_name="big", content="x" * 101))
        assert res["ok"] is False
        assert res["error"]["type"] == "file_limit_exceeded"

    def test_max_files_from_config(self):
        ctx = SimpleNamespace(config_dict={"memory_files_max_files": 2})
        state = _make_state({"journal": "j", "arch": "a"})
        tool = MemoryFiles(ctx=ctx, parent_state=state)
        res = _parse(tool(action="write", file_name="third", content="x"))
        assert res["ok"] is False
        assert res["error"]["type"] == "max_files_exceeded"

    def test_all_limits_from_config(self):
        ctx = SimpleNamespace(config_dict={
            "memory_files_max_file_chars": 200,
            "memory_files_total_budget_chars": 300,
            "memory_files_max_files": 5,
        })
        state = _make_state()
        tool = MemoryFiles(ctx=ctx, parent_state=state)
        assert tool.MAX_FILE_CHARS == 200
        assert tool.TOTAL_BUDGET_CHARS == 300
        assert tool.MAX_FILES == 5


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_no_state(self):
        tool = MemoryFiles(parent_state=None)
        res = _parse(tool(action="list"))
        assert res["ok"] is False
        assert res["error"]["type"] == "no_state"

    def test_invalid_action(self):
        res = _call(_make_state(), action="explode")
        assert res["ok"] is False
        assert res["error"]["type"] == "invalid_action"

    def test_default_file_name_is_journal(self):
        state = _make_state()
        res = _call(state, action="append", content="note")
        assert "journal" in state.memory_files

    def test_file_name_stripped_and_lowered(self):
        state = _make_state()
        res = _call(state, action="write", file_name=" Journal ", content="hi")
        assert res["ok"] is True
        assert "journal" in state.memory_files

    def test_state_without_memory_files_attr(self):
        state = SimpleNamespace()
        tool = MemoryFiles(parent_state=state)
        res = _parse(tool(action="list"))
        assert res["ok"] is True
        assert res["files"] == {}
        assert "hint" in res
