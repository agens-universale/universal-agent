import json
from types import SimpleNamespace

from tools.simple_tools.thread_notes import ThreadNotes


def test_thread_notes_no_state():
    data = json.loads(ThreadNotes(parent_state=None)(action="read"))
    assert data["ok"] is False
    assert data["error"]["type"] == "no_state"


def test_thread_notes_read_append_write():
    state = SimpleNamespace(notes_md="", call_metadata={"promptVariables": {}})
    tool = ThreadNotes(parent_state=state)
    read_empty = json.loads(tool(action="read"))
    assert read_empty["ok"] is True
    assert "Notes are empty" in read_empty["notes_md"]

    app = json.loads(tool(action="append", content="A"))
    assert app["ok"] is True and state.notes_md == "A"
    app2 = json.loads(tool(action="append", content="B"))
    assert app2["ok"] is True and state.notes_md == "A\n\nB"
    wr = json.loads(tool(action="write", content="X"))
    assert wr["ok"] is True and state.notes_md == "X"
    assert "notes_md" not in state.call_metadata.get("promptVariables", {})


def test_thread_notes_limits_and_validation():
    state = SimpleNamespace(notes_md="", call_metadata={"promptVariables": {}})
    tool = ThreadNotes(parent_state=state)
    bad = json.loads(tool(action="unknown"))
    assert bad["ok"] is False and bad["error"]["type"] == "invalid_action"
    empty = json.loads(tool(action="write", content="   "))
    assert empty["ok"] is False and empty["error"]["type"] == "empty_content"
    too_long = json.loads(tool(action="write", content="x" * (ThreadNotes.MAX_CHARS + 1)))
    assert too_long["ok"] is False and too_long["error"]["type"] == "limit_exceeded"
