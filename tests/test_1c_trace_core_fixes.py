"""Core fixes D-2 and D-3 from the 1C bench trace analysis."""
import hashlib
import json

from gc_core.states import State
from gc_core.tools.fs.append_to_file import AppendToFile
from gc_core.tools.fs.apply_file_edits import ApplyFileEdits
from gc_core.tools.fs.search_and_replace import SearchAndReplace
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from tests.helpers.tool_ctx import with_local_ctx


def _fresh_state():
    state = State.__new__(State)
    state.messages = []
    state.on_messages_evicted = None
    state.logger = None
    state.dialog_steps_offset = 0
    state.previous_response_id = None
    state.shared = None
    state.user_intent_log = []
    state.current_user_intent = []
    from gc_core.tools.session.stored_outputs.tool_call_outputs_store import ToolCallOutputsStore
    state.tool_call_outputs_store = ToolCallOutputsStore(max_entries=10)
    return state


# ---------- D-2 ----------

def test_compaction_emits_dropped_messages_via_callback():
    from gc_core.summarize.compaction import apply_summary_state_patch

    state = _fresh_state()
    archived: list = []
    state.on_messages_evicted = archived.extend

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

    assert archived == [step1, step2]
    assert state.messages[0] is system
    # second compaction appends via callback, not overwrite
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
    assert archived[:2] == [step1, step2]
    assert step3 in archived


def test_compaction_without_callback_does_not_retain_dropped():
    from gc_core.summarize.compaction import apply_summary_state_patch

    state = _fresh_state()
    system = {"role": "system", "content": "sys"}
    step1 = {"role": "user", "content": "u1"}
    state.messages = [system, step1]

    apply_summary_state_patch(
        state=state,
        raw_summary="summary text",
        last_dialog_steps="",
        memory_append_deltas=None,
        memory_replace_deltas=None,
        memory_append_touched=None,
        memory_replace_touched=None,
        summarized_messages_count=1,
        intent_log_preview_chars=1000,
    )

    assert not hasattr(state, "archived_messages")
    assert state.messages[0] is system
    assert len(state.messages) == 2


def test_compaction_callback_errors_do_not_fail_patch():
    from gc_core.summarize.compaction import apply_summary_state_patch

    state = _fresh_state()

    def _boom(_dropped):
        raise RuntimeError("sink failed")

    state.on_messages_evicted = _boom
    system = {"role": "system", "content": "sys"}
    state.messages = [system, {"role": "user", "content": "u1"}]

    apply_summary_state_patch(
        state=state,
        raw_summary="summary text",
        last_dialog_steps="",
        memory_append_deltas=None,
        memory_replace_deltas=None,
        memory_append_touched=None,
        memory_replace_touched=None,
        summarized_messages_count=1,
        intent_log_preview_chars=1000,
    )
    assert state.messages[0] is system
    assert len(state.messages) == 2


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
