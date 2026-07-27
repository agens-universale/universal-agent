"""Core fixes D-2 and D-3 from the 1C bench trace analysis."""
import hashlib
import json

from states import State
from tools.fs.append_to_file import AppendToFile
from tools.fs.apply_file_edits import ApplyFileEdits
from tools.fs.search_and_replace import SearchAndReplace
from tools.fs.write_to_file_tool import WriteToFile
from tests.helpers.tool_ctx import with_local_ctx


def _fresh_state():
    state = State.__new__(State)
    state.messages = []
    state.archived_messages = []
    from tools.session.stored_outputs.tool_call_outputs_store import (
        ToolCallOutputsStore,
    )
    state.tool_call_outputs_store = ToolCallOutputsStore(max_entries=10)
    return state


# ---------- D-2 ----------

def test_compaction_archives_dropped_messages():
    from summarize.compaction import apply_summary_state_patch

    state = _fresh_state()
    system = {"role": "system", "content": "sys"}
    step1 = {"role": "user", "content": "u1"}
    step2 = {"role": "assistant", "content": "a1"}
    state.messages = [system, step1, step2]

    apply_summary_state_patch(
        state=state,
        raw_summary="summary text",
        last_dialog_steps="",
        memory_append_deltas=None,
        memory_replace_deltas=None,
        memory_append_touched=None,
        memory_replace_touched=None,
        summarized_messages_count=2,
        intent_log_preview_chars=1000,
    )

    assert state.archived_messages == [step1, step2]
    assert state.messages[0] is system
    # second compaction appends, not overwrites
    step3 = {"role": "user", "content": "u2"}
    state.messages.append(step3)
    apply_summary_state_patch(
        state=state,
        raw_summary="summary 2",
        last_dialog_steps="",
        memory_append_deltas=None,
        memory_replace_deltas=None,
        memory_append_touched=None,
        memory_replace_touched=None,
        summarized_messages_count=2,
        intent_log_preview_chars=1000,
    )
    assert state.archived_messages[:2] == [step1, step2]
    assert step3 in state.archived_messages


# ---------- D-3 ----------

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_write_file_returns_sha256(tmp_path):
    p = tmp_path / "a.txt"
    tool = with_local_ctx(WriteToFile)
    res = json.loads(tool(path=str(p), content="Привет, мир\n"))
    assert res["ok"] is True
    assert res["sha256"] == _sha("Привет, мир\n")
    assert res["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()


def test_apply_file_edits_returns_sha256(tmp_path):
    p = tmp_path / "b.txt"
    p.write_text("hello world\n", encoding="utf-8")
    tool = with_local_ctx(ApplyFileEdits)
    res = json.loads(
        tool(path=str(p), edits=[{"search": "world", "replace": "там"}])
    )
    assert res["ok"] is True
    assert res["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()


def test_search_and_replace_returns_sha256(tmp_path):
    p = tmp_path / "c.txt"
    p.write_text("aaa bbb\n", encoding="utf-8")
    tool = with_local_ctx(SearchAndReplace)
    res = json.loads(tool(path=str(p), search="bbb", replace="ccc"))
    assert res["ok"] is True
    assert res["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()


def test_append_file_returns_sha256(tmp_path):
    p = tmp_path / "d.txt"
    p.write_text("start", encoding="utf-8")
    tool = with_local_ctx(AppendToFile)
    res = json.loads(tool(path=str(p), content="tail"))
    assert res["ok"] is True
    assert res["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
