import types

from summarize.working_memory_gateway import (
    WorkingMemoryDeltas,
    WorkingMemorySnapshot,
    apply_append_deltas,
    build_memory_deltas_format_repair_messages,
    parse_memory_deltas,
    parse_memory_deltas_with_repair,
    read_snapshot_from_state,
    render_persistent_state_for_prompt,
    render_working_memory_for_summary_message,
    resolve_persistent_state_block,
    should_attempt_memory_deltas_repair,
)


def test_read_snapshot_from_state_normalizes_registers():
    state = types.SimpleNamespace(
        shared=types.SimpleNamespace(
            working_memory={
                "goal": "  finish migration  ",
                "constraints": None,
                "state": "in progress",
                "knowledge": 42,
            }
        )
    )

    snapshot = read_snapshot_from_state(state)

    assert snapshot == WorkingMemorySnapshot(
        goal="finish migration",
        constraints="",
        state="in progress",
        knowledge="",
    )


def test_render_persistent_state_for_prompt_contains_all_registers():
    snapshot = WorkingMemorySnapshot(
        goal="G",
        constraints="C",
        state="S",
        knowledge="K",
    )

    rendered = render_persistent_state_for_prompt(snapshot)

    assert "<persistent_state>" in rendered
    assert "<working_memory>" in rendered
    assert "<goal>" in rendered and "</goal>" in rendered
    assert "<constraints>" in rendered and "</constraints>" in rendered
    assert "<state>" in rendered and "</state>" in rendered
    assert "<knowledge>" in rendered and "</knowledge>" in rendered
    assert "G" in rendered and "C" in rendered and "S" in rendered and "K" in rendered


def test_parse_memory_deltas_parses_valid_and_partial_registers():
    raw = """
noise
<state>New status line</state>
something else
<goal>
- finalize cutover
</goal>
"""

    deltas = parse_memory_deltas(raw)

    assert deltas == WorkingMemoryDeltas(
        goal="- finalize cutover",
        state="New status line",
    )


def test_parse_memory_deltas_returns_empty_on_broken_or_empty_payload():
    assert parse_memory_deltas("just text without tags") == WorkingMemoryDeltas()
    assert parse_memory_deltas("<goal></goal><state>   </state>") == WorkingMemoryDeltas()


def test_apply_append_deltas_uses_append_semantics():
    snapshot = WorkingMemorySnapshot(
        goal="existing goal",
        constraints="",
        state="existing state",
        knowledge="",
    )
    deltas = {
        "goal": "new goal delta",
        "constraints": "must keep tests green",
        "state": "",
        "knowledge": "verified fact",
    }

    updated = apply_append_deltas(snapshot, deltas)

    assert updated.goal == "existing goal\n\nnew goal delta"
    assert updated.constraints == "must keep tests green"
    assert updated.state == "existing state"
    assert updated.knowledge == "verified fact"


def test_render_working_memory_for_summary_message_skips_empty_registers():
    snapshot = WorkingMemorySnapshot(
        goal="finalize",
        constraints="",
        state="",
        knowledge="ref: docs_md/memory-files-to-working-memory-spec.md",
    )

    rendered = render_working_memory_for_summary_message(snapshot)

    assert rendered.startswith("<working_memory>")
    assert "<goal>" in rendered and "finalize" in rendered
    assert "<knowledge>" in rendered and "ref: docs_md/memory-files-to-working-memory-spec.md" in rendered
    assert "<constraints>" not in rendered
    assert "<state>" not in rendered
    assert rendered.strip().endswith("</working_memory>")


def test_should_attempt_memory_deltas_repair_when_large_unparsed_text():
    raw = "x" * 120
    deltas = WorkingMemoryDeltas()
    assert should_attempt_memory_deltas_repair(raw, deltas, min_chars_for_repair=100) is True


def test_should_not_attempt_memory_deltas_repair_when_short_or_parsed():
    assert should_attempt_memory_deltas_repair("short", WorkingMemoryDeltas(), min_chars_for_repair=100) is False
    assert (
        should_attempt_memory_deltas_repair(
            "x" * 120,
            WorkingMemoryDeltas(goal="parsed"),
            min_chars_for_repair=100,
        )
        is False
    )


def test_parse_memory_deltas_with_repair_invokes_callback_once():
    raw = "narrative text without tags " + ("x" * 120)
    calls = {"count": 0}

    def _repair(text: str) -> str:
        calls["count"] += 1
        assert text == raw
        return "<goal>recovered goal</goal>"

    deltas, attempted, repaired_raw = parse_memory_deltas_with_repair(
        raw,
        repair_fn=_repair,
        min_chars_for_repair=100,
    )

    assert attempted is True
    assert repaired_raw == "<goal>recovered goal</goal>"
    assert deltas == WorkingMemoryDeltas(goal="recovered goal")
    assert calls["count"] == 1


def test_build_memory_deltas_format_repair_messages_lists_register_tags():
    messages = build_memory_deltas_format_repair_messages("free text")
    assert messages[0]["role"] == "system"
    assert "<goal>" in messages[0]["content"]
    assert "<knowledge>" in messages[0]["content"]
    assert "free text" in messages[1]["content"]


def test_resolve_persistent_state_block_uses_explicit_block():
    state = types.SimpleNamespace(shared=types.SimpleNamespace(working_memory={"goal": "from state"}))
    explicit = "<persistent_state>explicit</persistent_state>"
    assert resolve_persistent_state_block(state, explicit) == explicit


def test_resolve_persistent_state_block_reads_from_state_when_none():
    state = types.SimpleNamespace(
        shared=types.SimpleNamespace(working_memory={"goal": "from state"})
    )
    resolved = resolve_persistent_state_block(state, None)
    assert "<persistent_state>" in resolved
    assert "from state" in resolved


def test_parse_memory_deltas_with_repair_skips_callback_when_not_needed():
    raw = "<state>ok</state>"
    calls = {"count": 0}

    def _repair(_text: str) -> str:
        calls["count"] += 1
        return "<goal>unexpected</goal>"

    deltas, attempted, repaired_raw = parse_memory_deltas_with_repair(
        raw,
        repair_fn=_repair,
        min_chars_for_repair=100,
    )

    assert attempted is False
    assert repaired_raw == raw
    assert deltas == WorkingMemoryDeltas(state="ok")
    assert calls["count"] == 0
