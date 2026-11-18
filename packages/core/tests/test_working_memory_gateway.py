import types

from gc_core.summarize.working_memory_gateway import (
    _REGISTER_BODY_PATTERNS,
    REGISTER_MERGE_MODES,
    REGISTER_ORDER,
    SESSION_MEMORY_REGISTERS,
    WorkingMemoryDeltas,
    WorkingMemorySnapshot,
    apply_append_deltas,
    apply_memory_patch,
    apply_replace_deltas,
    apply_summary_memory_patch,
    build_memory_deltas_format_repair_messages,
    coerce_working_memory_deltas,
    extract_register_bodies,
    parse_memory_deltas,
    parse_memory_deltas_with_repair,
    read_snapshot_from_state,
    render_persistent_state_for_prompt,
    render_session_memory_for_summary_message,
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


def test_register_body_patterns_cover_canon():
    assert set(_REGISTER_BODY_PATTERNS) == set(REGISTER_ORDER)
    assert len(REGISTER_ORDER) == len(SESSION_MEMORY_REGISTERS)


def test_extract_register_bodies_unescaped_ampersand_and_last_match():
    raw = "<goal>a & b</goal><state>first</state><state>second</state>"
    bodies = extract_register_bodies(raw)
    assert bodies["goal"] == "a & b"
    assert bodies["state"] == "second"


def test_final_register_block_excludes_open_tag_in_preamble():
    raw = (
        "<knowledge>: draft notes\n<goal>: draft objective\n"
        "Let me format this properly:\n</think>\n"
        "<goal>final goal</goal>\n<constraints>keep x < 5</constraints>\n"
        "<state>next step</state>\n<knowledge>final fact</knowledge>"
    )
    old = WorkingMemorySnapshot(knowledge="old fact")

    deltas, present = parse_memory_deltas(raw, full_replace=True)
    updated = apply_summary_memory_patch(old, deltas, replace_touched=present)

    assert updated.goal == "final goal"
    assert updated.constraints == "keep x < 5"
    assert updated.state == "next step"
    assert updated.knowledge == "final fact"


def test_partial_register_patch_and_empty_clear_remain_valid():
    old = WorkingMemorySnapshot(goal="old goal", knowledge="old fact")
    deltas, present = parse_memory_deltas(
        "preface\n<goal>new goal</goal><knowledge></knowledge>"
    )

    updated = apply_summary_memory_patch(old, deltas, replace_touched=present)

    assert updated.goal == "new goal"
    assert updated.knowledge == ""
    assert present == frozenset({"goal", "knowledge"})


def test_register_body_keeps_generic_type_parameters():
    value = "Use std::vector<int> and preserve the result."

    deltas, present = parse_memory_deltas(f"<knowledge>{value}</knowledge>")

    assert present == frozenset({"knowledge"})
    assert deltas.knowledge == value


def test_register_body_keeps_markdown_quoted_register_tag():
    value = "Keep the `<state>` element in the exported document."
    raw = (
        "<goal>Validate XML export</goal>"
        "<constraints>Keep its schema</constraints>"
        "<state>Validate the rendered XML next</state>"
        f"<knowledge>{value}</knowledge>"
    )

    deltas, present = parse_memory_deltas(raw)

    assert present == frozenset(REGISTER_ORDER)
    assert deltas.knowledge == value


def test_whole_response_fence_wraps_valid_register_patch():
    raw = "```xml\n<knowledge>Use the newer offline cache.</knowledge>\n```"

    deltas, present = parse_memory_deltas(raw)

    assert present == frozenset({"knowledge"})
    assert deltas.knowledge == "Use the newer offline cache."


def test_fenced_example_in_explanation_is_not_applied():
    raw = (
        "For example:\n```xml\n<knowledge>illustration</knowledge>\n```\n"
        "Now decide what to store."
    )

    deltas, present = parse_memory_deltas(raw)

    assert present == frozenset()
    assert deltas.is_empty()


def test_final_fenced_register_block_after_intro_is_applied():
    raw = (
        "Here is the updated memory:\n```xml\n"
        "<goal>new goal</goal><knowledge>new fact</knowledge>\n```"
    )

    deltas, present = parse_memory_deltas(raw)

    assert present == frozenset({"goal", "knowledge"})
    assert deltas.goal == "new goal"
    assert deltas.knowledge == "new fact"


def test_single_fenced_register_block_with_text_on_both_sides_is_applied():
    raw = (
        "Here is the updated memory:\n```xml\n"
        "<goal>new goal</goal><knowledge>new fact</knowledge>\n```\n"
        "The registers above are ready."
    )

    deltas, present = parse_memory_deltas(raw)

    assert present == frozenset({"goal", "knowledge"})
    assert deltas.goal == "new goal"
    assert deltas.knowledge == "new fact"


def test_fenced_example_inside_register_body_stays_literal():
    value = "Run:\n```xml\n<state>example</state>\n```\nKeep this text."

    deltas, present = parse_memory_deltas(f"<knowledge>{value}</knowledge>")

    assert present == frozenset({"knowledge"})
    assert deltas.knowledge == value


def test_text_mentions_between_and_after_registers_keep_entire_patch():
    raw = (
        "<goal>G</goal><constraints>C</constraints>\n"
        "Note: prints <stdout> only.\n"
        "<state>S</state><knowledge>K</knowledge>\n"
        "I updated <knowledge> with verified facts."
    )

    deltas, present = parse_memory_deltas(raw)

    assert present == frozenset(REGISTER_ORDER)
    assert deltas.to_dict() == {
        "goal": "G",
        "constraints": "C",
        "state": "S",
        "knowledge": "K",
    }


def test_inline_code_caption_before_register_does_not_drop_update():
    raw = (
        "<goal>new goal</goal>"
        "<knowledge>Keep `</think>` literal and `run` in notes.</knowledge>\n"
        "`state notes` <state>new state</state>"
    )
    old = WorkingMemorySnapshot(state="old state")

    deltas, present = parse_memory_deltas(raw)
    updated = apply_summary_memory_patch(old, deltas, replace_touched=present)

    assert present == frozenset({"goal", "knowledge", "state"})
    assert updated.state == "new state"
    assert updated.knowledge == "Keep `</think>` literal and `run` in notes."


def test_escaped_backtick_does_not_hide_later_code_span():
    raw = (
        "<knowledge>Keep \\` literally in the source.</knowledge>"
        "<state>Use `<goal>` as a label.</state>"
    )
    old = WorkingMemorySnapshot(knowledge="old fact", state="old state")

    deltas, present = parse_memory_deltas(raw)
    updated = apply_summary_memory_patch(old, deltas, replace_touched=present)

    assert present == frozenset({"knowledge", "state"})
    assert updated.knowledge == "Keep \\` literally in the source."
    assert updated.state == "Use `<goal>` as a label."


def test_even_backslashes_leave_backtick_as_code_delimiter():
    value = r"Keep \\`<state>` as a literal label."

    deltas, present = parse_memory_deltas(f"<knowledge>{value}</knowledge>")

    assert present == frozenset({"knowledge"})
    assert deltas.knowledge == value


def test_backslash_before_closing_backtick_keeps_literal_tag_in_code():
    value = "Keep `<goal> ends with \\` literally."
    raw = f"<knowledge>{value}</knowledge><state>new state</state>"

    deltas, present = parse_memory_deltas(raw)

    assert present == frozenset({"knowledge", "state"})
    assert deltas.knowledge == value
    assert deltas.state == "new state"


def test_inline_code_example_caption_does_not_replace_register():
    raw = "<state>real state</state>\n`Example`: <state>example state</state>"

    deltas, present = parse_memory_deltas(raw)

    assert present == frozenset({"state"})
    assert deltas.state == "real state"


def test_backticks_do_not_span_across_register_boundaries():
    raw = (
        "<goal>Run `make.</goal>"
        "<constraints>Keep the existing `schema`.</constraints>"
        "<state>Next step</state><knowledge>Verified fact</knowledge>"
    )

    deltas, present = parse_memory_deltas(raw)

    assert present == frozenset(REGISTER_ORDER)
    assert deltas.goal == "Run `make."
    assert deltas.constraints == "Keep the existing `schema`."


def test_damaged_structural_tail_does_not_apply_partial_patch():
    prefix = (
        "<goal>new goal</goal><constraints>new rule</constraints>"
        "<state>new step</state><knowledge>new fact</knowledge>"
    )

    for tail in ("\n<knowledge>unfinished", "\n<knowledge"):
        deltas, present = parse_memory_deltas(prefix + tail)

        assert present == frozenset()
        assert deltas.is_empty()


def test_unknown_structural_tag_between_registers_does_not_apply_tail():
    raw = (
        "<goal>G</goal><constraints>C</constraints>\n"
        "<stdout>unexpected</stdout>\n"
        "<state>S</state><knowledge>K</knowledge>"
    )

    deltas, present = parse_memory_deltas(raw)

    assert present == frozenset()
    assert deltas.is_empty()


def test_terminal_fenced_example_is_not_a_memory_patch():
    raw = "For example:\n```xml\n<knowledge>illustration</knowledge>\n```"

    deltas, present = parse_memory_deltas(raw)

    assert present == frozenset()
    assert deltas.is_empty()


def test_multiple_fenced_register_blocks_are_ambiguous():
    raw = (
        "```xml\n<knowledge>draft</knowledge>\n```\n"
        "```xml\n<knowledge>final</knowledge>\n```"
    )

    deltas, present = parse_memory_deltas(raw)

    assert present == frozenset()
    assert deltas.is_empty()


def test_register_body_keeps_literal_think_closing_tag():
    value = "Generic <Result> comes from transport; keep </think> literal."

    deltas, present = parse_memory_deltas(f"<knowledge>{value}</knowledge>")

    assert present == frozenset({"knowledge"})
    assert deltas.knowledge == value


def test_final_register_body_keeps_literal_think_after_preamble():
    value = "Use std::vector<int> and keep </think> in the fixture."
    raw = (
        "Format the <knowledge>: register as follows.\n</think>\n"
        f"<knowledge>{value}</knowledge>"
    )

    deltas, present = parse_memory_deltas(raw)

    assert present == frozenset({"knowledge"})
    assert deltas.knowledge == value


def test_unclosed_or_nested_registers_do_not_apply():
    old = WorkingMemorySnapshot(knowledge="old fact")
    for raw in (
        "<knowledge>draft <goal>inner</goal></knowledge>",
        "<goal>unfinished <state>valid</state>",
        "<goal>new goal</goal><knowledge>unfinished",
        "<goal>new goal</goal><unknown>unexpected</unknown>",
    ):
        deltas, present = parse_memory_deltas(raw)
        updated = apply_summary_memory_patch(
            old, deltas, replace_touched=present
        )
        assert updated == old
        assert present == frozenset()


def test_render_persistent_state_roundtrip_all_registers():
    snapshot = WorkingMemorySnapshot(
        **{name: f"body-{name}" for name in REGISTER_ORDER}
    )
    rendered = render_persistent_state_for_prompt(snapshot)
    extracted = extract_register_bodies(rendered)
    for name in REGISTER_ORDER:
        assert extracted[name] == f"body-{name}"


def test_render_persistent_state_for_prompt_contains_all_registers():
    snapshot = WorkingMemorySnapshot(
        goal="G",
        constraints="C",
        state="S",
        knowledge="K",
    )

    rendered = render_persistent_state_for_prompt(snapshot)

    assert "<persistent_state>" in rendered
    assert "<session_memory>" in rendered
    for name in REGISTER_ORDER:
        assert f"<{name}>" in rendered and f"</{name}>" in rendered
    assert (
        "G" in rendered
        and "C" in rendered
        and "S" in rendered
        and "K" in rendered
    )


def test_coerce_working_memory_deltas_accepts_dto_and_mapping():
    dto = WorkingMemoryDeltas(goal="g", state="s")
    assert coerce_working_memory_deltas(dto) is dto
    assert coerce_working_memory_deltas(
        {"goal": " g ", "state": "s"}
    ) == WorkingMemoryDeltas(
        goal="g",
        state="s",
    )
    assert coerce_working_memory_deltas(None) == WorkingMemoryDeltas()


def test_parse_memory_deltas_parses_valid_and_partial_registers():
    raw = """
noise
<state>New status line</state>
something else
<goal>
- finalize cutover
</goal>
"""

    deltas, present = parse_memory_deltas(raw)

    assert deltas == WorkingMemoryDeltas(
        goal="- finalize cutover",
        state="New status line",
    )
    assert present == frozenset({"goal", "state"})


def test_parse_memory_deltas_returns_empty_on_broken_or_empty_payload():
    deltas, present = parse_memory_deltas("just text without tags")
    assert deltas == WorkingMemoryDeltas()
    assert present == frozenset()

    deltas, present = parse_memory_deltas("<goal></goal><state>   </state>")
    assert deltas == WorkingMemoryDeltas(goal="", state="")
    assert present == frozenset({"goal", "state"})


def test_parse_memory_deltas_empty_tag_clears_register():
    deltas, present = parse_memory_deltas(
        "<knowledge></knowledge><constraints>  </constraints><knowledge>fact</knowledge>"
    )
    assert deltas == WorkingMemoryDeltas(constraints="", knowledge="fact")
    assert present == frozenset({"constraints", "knowledge"})


def test_register_merge_modes_all_replace():
    assert dict.fromkeys(REGISTER_ORDER, "replace") == REGISTER_MERGE_MODES


def test_apply_append_deltas_is_noop_when_all_replace():
    snapshot = WorkingMemorySnapshot(
        goal="existing goal",
        constraints="",
        state="existing state",
        knowledge="",
    )
    deltas = {
        "goal": "new goal delta",
        "constraints": "must keep tests green",
        "state": "new state delta",
        "knowledge": "verified fact",
    }

    updated = apply_append_deltas(snapshot, deltas)

    assert updated == snapshot


def test_apply_replace_deltas_replaces_any_register():
    snapshot = WorkingMemorySnapshot(
        goal="existing goal",
        constraints="existing constraints",
        state="existing state",
        knowledge="existing knowledge",
    )
    deltas = {
        "goal": "new goal snapshot",
        "constraints": "new constraints snapshot",
        "state": "new state snapshot",
        "knowledge": "new knowledge snapshot",
    }

    updated = apply_replace_deltas(snapshot, deltas)

    assert updated.goal == "new goal snapshot"
    assert updated.state == "new state snapshot"
    assert updated.constraints == "new constraints snapshot"
    assert updated.knowledge == "new knowledge snapshot"


def test_apply_replace_deltas_clears_register_when_touched_and_empty():
    snapshot = WorkingMemorySnapshot(
        goal="old goal",
        state="old state",
    )
    deltas = WorkingMemoryDeltas(goal="", state="done")

    updated = apply_replace_deltas(
        snapshot,
        deltas,
        touched=frozenset({"goal", "state"}),
    )

    assert updated.goal == ""
    assert updated.state == "done"


def test_parse_memory_deltas_full_replace_clears_empty_constraints():
    deltas, present = parse_memory_deltas(
        "<constraints></constraints><knowledge>fact</knowledge>",
        full_replace=True,
    )
    assert deltas == WorkingMemoryDeltas(constraints="", knowledge="fact")
    assert present == frozenset({"constraints", "knowledge"})


def test_apply_summary_memory_patch_skips_registers_missing_from_llm_output():
    snapshot = WorkingMemorySnapshot(
        goal="old goal",
        constraints="stale rule",
        state="stale TODO",
        knowledge="stale fact",
    )
    deltas, present = parse_memory_deltas(
        "<goal>new goal</goal><state>fresh TODO</state>",
        full_replace=True,
    )
    assert present == frozenset({"goal", "state"})
    updated = apply_summary_memory_patch(
        snapshot, deltas, replace_touched=present
    )
    assert updated.goal == "new goal"
    assert updated.state == "fresh TODO"
    assert updated.constraints == "stale rule"
    assert updated.knowledge == "stale fact"


def test_apply_summary_memory_patch_replaces_all_registers():
    snapshot = WorkingMemorySnapshot(
        goal="old goal",
        constraints="stale rule",
        state="stale TODO",
        knowledge="stale fact",
    )
    replace_deltas = {
        "goal": "new goal",
        "constraints": "new rule",
        "state": "fresh TODO",
        "knowledge": "new fact",
    }
    updated = apply_summary_memory_patch(
        snapshot,
        replace_deltas,
        replace_touched=frozenset(replace_deltas.keys()),
    )
    assert updated.goal == "new goal"
    assert updated.constraints == "new rule"
    assert updated.state == "fresh TODO"
    assert updated.knowledge == "new fact"


def test_apply_memory_patch_combines_append_and_replace():
    snapshot = WorkingMemorySnapshot(
        goal="old goal",
        constraints="old constraints",
        state="stale TODO",
        knowledge="old knowledge",
    )
    append_deltas = {
        "constraints": "new rule",
        "knowledge": "new fact",
        "state": "must not append",
    }
    replace_deltas = {
        "goal": "fresh goal",
        "state": "fresh state",
        "knowledge": "must not replace",
    }

    updated = apply_memory_patch(snapshot, append_deltas, replace_deltas)

    assert updated.goal == "fresh goal"
    assert updated.state == "fresh state"
    assert updated.constraints == "new rule"
    assert updated.knowledge == "must not replace"


def test_render_session_memory_for_summary_message_skips_empty_registers():
    snapshot = WorkingMemorySnapshot(
        goal="finalize",
        constraints="",
        state="",
        knowledge="ref: docs_md/memory-files-to-working-memory-spec.md",
    )

    rendered = render_session_memory_for_summary_message(snapshot)

    assert rendered.startswith("<session_memory>")
    assert "<goal>" in rendered and "finalize" in rendered
    assert (
        "<knowledge>" in rendered
        and "ref: docs_md/memory-files-to-working-memory-spec.md" in rendered
    )
    assert "<constraints>" not in rendered
    assert "<state>" not in rendered
    assert rendered.strip().endswith("</session_memory>")


def test_should_attempt_memory_deltas_repair_when_large_unparsed_text():
    raw = "x" * 120
    deltas = WorkingMemoryDeltas()
    assert (
        should_attempt_memory_deltas_repair(
            raw, deltas, min_chars_for_repair=100
        )
        is True
    )


def test_should_not_attempt_memory_deltas_repair_when_short_or_parsed():
    assert (
        should_attempt_memory_deltas_repair(
            "short", WorkingMemoryDeltas(), min_chars_for_repair=100
        )
        is False
    )
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

    deltas, present, attempted, repaired_raw = parse_memory_deltas_with_repair(
        raw,
        repair_fn=_repair,
        min_chars_for_repair=100,
    )

    assert attempted is True
    assert repaired_raw == "<goal>recovered goal</goal>"
    assert deltas == WorkingMemoryDeltas(goal="recovered goal")
    assert present == frozenset({"goal"})
    assert calls["count"] == 1


def test_build_memory_deltas_format_repair_messages_lists_register_tags():
    messages = build_memory_deltas_format_repair_messages("free text")
    assert messages[0]["role"] == "system"
    assert "<goal>" in messages[0]["content"]
    assert "<knowledge>" in messages[0]["content"]
    assert "free text" in messages[1]["content"]


def test_resolve_persistent_state_block_uses_explicit_block():
    state = types.SimpleNamespace(
        shared=types.SimpleNamespace(working_memory={"goal": "from state"})
    )
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

    deltas, present, attempted, repaired_raw = parse_memory_deltas_with_repair(
        raw,
        repair_fn=_repair,
        min_chars_for_repair=100,
    )

    assert attempted is False
    assert repaired_raw == raw
    assert deltas == WorkingMemoryDeltas(state="ok")
    assert present == frozenset({"state"})
    assert calls["count"] == 0
