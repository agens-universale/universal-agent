import json
import sys
import types
from types import SimpleNamespace


def _stub_optional_web_parsers() -> None:
    if "trafilatura" not in sys.modules:
        mod = types.ModuleType("trafilatura")
        mod.extract = lambda *args, **kwargs: None
        sys.modules["trafilatura"] = mod

    if "readability" not in sys.modules:
        mod = types.ModuleType("readability")

        class _Document:
            def __init__(self, *_args, **_kwargs):
                pass

            def summary(self, html_partial=True):
                return ""

        mod.Document = _Document
        sys.modules["readability"] = mod


_stub_optional_web_parsers()

from processing_responses import process_tool_calls
from persistence.state_storage import StateStorage
from states import State
from tools.stored_outputs.read_stored_tool_output import ReadStoredToolOutput
from tools.stored_outputs.tool_call_outputs_store import (
    ToolCallOutputRecord,
    ToolCallOutputsStore,
)
from tools.stored_outputs.wrapping import maybe_store_and_wrap_tool_output


def _make_state() -> State:
    return State(
        verbose=False,
        model="test-model",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="v1",
        use_server_subagents=False,
    )


def test_wrap_short_output_returns_original_text():
    state = _make_state()
    cfg = {"tool_output_inline_max_chars": 5, "stored_tool_outputs_max_entries": 10}
    res = maybe_store_and_wrap_tool_output(
        "abc",
        call_id="call_1",
        tool_name="some_tool",
        state=state,
        config=cfg,
    )
    assert res == "abc"
    assert len(state.tool_call_outputs_store) == 0


def test_wrap_long_output_stores_and_returns_envelope():
    state = _make_state()
    cfg = {"tool_output_inline_max_chars": 5, "stored_tool_outputs_max_entries": 10}
    res = maybe_store_and_wrap_tool_output(
        "abcdefghij",
        call_id="call_2",
        tool_name="some_tool",
        state=state,
        config=cfg,
    )
    payload = json.loads(res)
    assert payload["type"] == "stored_tool_output"
    assert payload["stored"] is True
    assert payload["from_original_call_id"] == "call_2"
    assert payload["source_tool"] == "some_tool"
    assert payload["content"] == "abcde"
    assert payload["next_offset"] == 5
    assert payload["truncated"] is True
    assert len(state.tool_call_outputs_store) == 1
    output_ref = payload["output_ref"]
    assert state.tool_call_outputs_store.contains(output_ref)


def test_wrap_respects_feature_off_and_skip_self_tool():
    state = _make_state()
    state.tool_call_outputs_store.set_max_entries(0)
    off_cfg = {"tool_output_inline_max_chars": 5, "stored_tool_outputs_max_entries": 10}
    res_off = maybe_store_and_wrap_tool_output(
        "abcdefghij",
        call_id="call_3",
        tool_name="some_tool",
        state=state,
        config=off_cfg,
    )
    assert res_off == "abcdefghij"

    on_cfg = {"tool_output_inline_max_chars": 5, "stored_tool_outputs_max_entries": 10}
    res_self = maybe_store_and_wrap_tool_output(
        '{"tool":"read_stored_tool_output"}',
        call_id="call_4",
        tool_name="read_stored_tool_output",
        state=state,
        config=on_cfg,
    )
    assert res_self == '{"tool":"read_stored_tool_output"}'


def test_store_lru_evicts_old_and_read_updates_recency():
    store = ToolCallOutputsStore(max_entries=2)
    r1 = ToolCallOutputRecord("r1", "t", "c1", 1, "a")
    r2 = ToolCallOutputRecord("r2", "t", "c2", 1, "b")
    r3 = ToolCallOutputRecord("r3", "t", "c3", 1, "c")
    store.put(r1)
    store.put(r2)
    # touch r1 so r2 becomes LRU
    assert store.get("r1") is r1
    evicted = store.put(r3)
    assert evicted is r2
    assert store.peek("r1") is r1
    assert store.peek("r2") is None
    assert store.peek("r3") is r3


def test_read_stored_tool_output_pagination_and_unicode():
    state = _make_state()
    state.tool_call_outputs_store.put(
        ToolCallOutputRecord(
            output_ref="ref-1",
            source_tool="tool_x",
            call_id="call_x",
            total_chars=len("A😀BC"),
            text="A😀BC",
        )
    )
    tool = ReadStoredToolOutput(state=state, config_dict={"tool_output_inline_max_chars": 2})
    page1 = json.loads(tool(output_ref="ref-1", offset=0, limit=10))
    assert page1["ok"] is True
    assert page1["limit"] == 2
    assert page1["content"] == "A😀"
    assert page1["next_offset"] == 2
    assert page1["truncated"] is True
    page2 = json.loads(tool(output_ref="ref-1", offset=2, limit=2))
    assert page2["content"] == "BC"
    assert page2["truncated"] is False
    assert page2["next_offset"] == 4


def test_read_stored_tool_output_updates_lru_recency_before_next_put():
    state = _make_state()
    state.tool_call_outputs_store.set_max_entries(2)
    state.tool_call_outputs_store.put(
        ToolCallOutputRecord(
            output_ref="r1",
            source_tool="tool_a",
            call_id="call_a",
            total_chars=6,
            text="abcdef",
        )
    )
    state.tool_call_outputs_store.put(
        ToolCallOutputRecord(
            output_ref="r2",
            source_tool="tool_b",
            call_id="call_b",
            total_chars=6,
            text="ghijkl",
        )
    )

    # Read r1 through the tool: this must move r1 to MRU.
    tool = ReadStoredToolOutput(state=state, config_dict={"tool_output_inline_max_chars": 5})
    page = json.loads(tool(output_ref="r1", offset=0, limit=2))
    assert page["ok"] is True
    assert page["output_ref"] == "r1"

    evicted = state.tool_call_outputs_store.put(
        ToolCallOutputRecord(
            output_ref="r3",
            source_tool="tool_c",
            call_id="call_c",
            total_chars=6,
            text="mnopqr",
        )
    )
    # If read updated recency correctly, r2 (not r1) is evicted.
    assert evicted is not None
    assert evicted.output_ref == "r2"
    assert state.tool_call_outputs_store.contains("r1")
    assert not state.tool_call_outputs_store.contains("r2")
    assert state.tool_call_outputs_store.contains("r3")


def test_read_stored_tool_output_invalid_and_not_found():
    state = _make_state()
    tool = ReadStoredToolOutput(state=state, config_dict={"tool_output_inline_max_chars": 5})
    bad = json.loads(tool(output_ref="", offset=0, limit=1))
    assert bad["ok"] is False
    assert bad["error"]["type"] == "invalid_argument"
    assert bad["next_offset"] == 0
    missing = json.loads(tool(output_ref="missing", offset=0, limit=1))
    assert missing["ok"] is False
    assert missing["stored"] is False
    assert missing["error"]["type"] == "not_found"
    assert missing["next_offset"] == 0


def test_read_stored_tool_output_not_found_when_store_missing_has_recover_by():
    # Simulate a "state without store" (store is None in the tool).
    state = SimpleNamespace()
    tool = ReadStoredToolOutput(state=state, config_dict={"tool_output_inline_max_chars": 5})
    payload = json.loads(tool(output_ref="missing", offset=0, limit=1))
    assert payload["ok"] is False
    assert payload["stored"] is False
    assert payload["error"]["type"] == "not_found"
    assert payload["error"]["recover_by"] == "reinvoke_source_tool"
    assert "re-invoke the original source tool" in payload["error"]["message"].lower()
    assert payload["next_offset"] == 0


def test_read_stored_tool_output_error_paths_have_zero_next_offset():
    state = _make_state()
    tool = ReadStoredToolOutput(state=state, config_dict={"tool_output_inline_max_chars": 5})

    not_int_limit = json.loads(tool(output_ref="ref", offset=1, limit="x"))  # type: ignore[arg-type]
    assert not_int_limit["ok"] is False
    assert not_int_limit["error"]["type"] == "invalid_argument"
    assert not_int_limit["next_offset"] == 0

    negative_offset = json.loads(tool(output_ref="ref", offset=-1, limit=1))
    assert negative_offset["ok"] is False
    assert negative_offset["error"]["type"] == "invalid_argument"
    assert negative_offset["next_offset"] == 0

    zero_limit = json.loads(tool(output_ref="ref", offset=3, limit=0))
    assert zero_limit["ok"] is False
    assert zero_limit["error"]["type"] == "invalid_argument"
    assert zero_limit["next_offset"] == 0


def test_not_found_after_real_lru_eviction_has_expected_error_envelope():
    state = _make_state()
    state.tool_call_outputs_store.set_max_entries(1)
    state.tool_call_outputs_store.put(
        ToolCallOutputRecord(
            output_ref="evicted_ref",
            source_tool="tool_a",
            call_id="call_a",
            total_chars=6,
            text="abcdef",
        )
    )
    # Force real eviction by inserting second record with max_entries=1.
    state.tool_call_outputs_store.put(
        ToolCallOutputRecord(
            output_ref="alive_ref",
            source_tool="tool_b",
            call_id="call_b",
            total_chars=6,
            text="ghijkl",
        )
    )

    tool = ReadStoredToolOutput(state=state, config_dict={"tool_output_inline_max_chars": 5})
    payload = json.loads(tool(output_ref="evicted_ref", offset=0, limit=5))

    assert payload["tool"] == "read_stored_tool_output"
    assert payload["type"] == "stored_tool_output"
    assert payload["ok"] is False
    assert payload["stored"] is False
    assert payload["reason"] == "source_tool_output_too_large"
    assert payload["output_ref"] == "evicted_ref"
    assert payload["next_offset"] == 0
    assert payload["error"]["type"] == "not_found"
    assert "re-invoke the original source tool" in payload["error"]["message"].lower()
    assert payload["error"]["recover_by"] == "reinvoke_source_tool"
    assert "re-invoke source_tool" in payload["hint"].lower()


def test_process_tool_calls_wraps_long_tool_output(monkeypatch):
    state = _make_state()
    ctx = SimpleNamespace(
        config_dict={
            "tool_output_inline_max_chars": 5,
            "stored_tool_outputs_max_entries": 10,
        }
    )

    def _fake_apply_tool(**kwargs):
        return "abcdefghij"

    monkeypatch.setattr("processing_responses.apply_tool", _fake_apply_tool)
    out = process_tool_calls(
        messages=[
            {
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_42",
                "name": "some_tool",
                "arguments": "{}",
            }
        ],
        state=state,
        ctx=ctx,
        run_agent_func=lambda *_args, **_kwargs: None,
    )
    assert len(out) == 1
    payload = json.loads(out[0]["output"])
    assert payload["type"] == "stored_tool_output"
    assert payload["from_original_call_id"] == "call_42"


def test_state_serialization_roundtrip_restores_store_and_lru_order(tmp_path):
    state = _make_state()
    state.tool_call_outputs_store.set_max_entries(2)
    state.tool_call_outputs_store.put(
        ToolCallOutputRecord(
            output_ref="r1",
            source_tool="t",
            call_id="c1",
            total_chars=len("A😀"),
            text="A😀",
        )
    )
    state.tool_call_outputs_store.put(
        ToolCallOutputRecord(
            output_ref="r2",
            source_tool="t",
            call_id="c2",
            total_chars=len("BC"),
            text="BC",
        )
    )
    # Now r2 should be LRU, r1 should be MRU.
    assert state.tool_call_outputs_store.get("r1") is not None

    storage = StateStorage()
    storage.add_state(state)
    dump_path = tmp_path / "state_dump.json"
    storage.save_to_file(str(dump_path), indent=2)

    loaded = StateStorage()
    loaded.load_from_file(str(dump_path))
    restored_state = loaded.get_state_by_id(state.state_id)
    assert restored_state is not None

    assert restored_state.tool_call_outputs_store.contains("r1")
    assert restored_state.tool_call_outputs_store.contains("r2")
    r3 = ToolCallOutputRecord(
        output_ref="r3",
        source_tool="t",
        call_id="c3",
        total_chars=1,
        text="Z",
    )
    evicted = restored_state.tool_call_outputs_store.put(r3)
    assert evicted is not None
    assert evicted.output_ref == "r2"


def test_feature_off_k_zero_clears_store_and_disables_read():
    state = _make_state()
    state.tool_call_outputs_store.set_max_entries(2)
    state.tool_call_outputs_store.put(
        ToolCallOutputRecord(
            output_ref="ref-1",
            source_tool="tool_x",
            call_id="call_x",
            total_chars=3,
            text="abc",
        )
    )
    assert len(state.tool_call_outputs_store) == 1

    # Hard-off mode: switching to K==0 clears store.
    state.tool_call_outputs_store.set_max_entries(0)
    assert len(state.tool_call_outputs_store) == 0
    assert state.tool_call_outputs_store.is_disabled() is True

    tool = ReadStoredToolOutput(state=state, config_dict={"tool_output_inline_max_chars": 5})
    payload = json.loads(tool(output_ref="ref-1", offset=0, limit=1))
    assert payload["ok"] is False
    assert payload["stored"] is False
    assert payload["error"]["type"] == "not_found"
    assert payload["error"]["recover_by"] == "reinvoke_source_tool"
    assert payload["next_offset"] == 0
