"""Binding constraints survive compaction when the memory patch misses them."""

import types
from pathlib import Path

import pytest
from gc_core.summarize import llm
from gc_core.summarize.compaction import arun_summary_compaction
from gc_core.summarize.summary_spec import assemble_summary_xml
from gc_core.summarize.working_memory_gateway import (
    WorkingMemorySnapshot,
    render_session_memory_for_summary_message,
)
from gc_testkit.paths import REPO
from gc_testkit.settings import fake_ctx

SUMMARY = assemble_summary_xml(
    {
        "task_and_context": "Continue extracting receipt data into the workbook.",
        "open_tasks": "mode: execution\n- [TODO] Verify all rows.",
        "do_not_redo": "n/a",
        "last_verified_facts": "The extraction script exists in the workspace.",
        "active_constraints": "Keep the results sheet and the exact column order.",
        "recent_artifacts": "The workbook is the output artifact.",
    }
)

# An anonymized memory-patch response with no register tags.
SAVED_PATCH_RESPONSE = """The three edits were applied, but there's a remaining bug: `extract_total` references `_bucket()` which doesn't exist yet — it will NameError on re-run. Let me inspect the current function and fix it properly.

<function_call>
<function_call>
{process: fs_read_file, args: {"path": "/workspace/extract.py", "offset": 100, "limit": 55, "add_line_numbers": true, "reason": "Inspect current extract_total to fix _bucket wiring}}
</function>
</tool_call>"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "patch_response",
        "expected_memory",
        "in_summary",
        "active_constraint",
    ),
    [
        (
            SAVED_PATCH_RESPONSE,
            "",
            True,
            "Keep the results sheet and the exact column order.",
        ),
        (
            "<state>Verify the workbook rows.</state>",
            "",
            True,
            "Keep the results sheet and the exact column order.",
        ),
        (
            "<constraints>Keep the results sheet and the exact column order.</constraints>",
            "Keep the results sheet and the exact column order.",
            False,
            "Keep the results sheet and the exact column order.",
        ),
        (
            "<constraints>Never edit files in output/tmp/</constraints>",
            "Never edit files in output/tmp/",
            True,
            "Never edit files in output/",
        ),
    ],
    ids=[
        "saved-empty-patch",
        "partial-state-patch",
        "healthy-constraints-patch",
        "narrower-memory-rule",
    ],
)
async def test_compaction_keeps_constraints_in_sent_message(
    monkeypatch,
    patch_response,
    expected_memory,
    in_summary,
    active_constraint,
):
    from gc_core.summarize import (
        compaction,
        memory_suggestions,
        thread_summarizer,
    )

    assert Path(compaction.__file__).resolve().is_relative_to(REPO.resolve())

    state = types.SimpleNamespace(
        default_model="test-model",
        thread_id="constraints-regression",
        shared=types.SimpleNamespace(working_memory={}),
        dialog_steps_offset=0,
        previous_response_id=None,
        user_intent_log=[],
        current_user_intent=[],
        messages=[{"role": "user", "content": "Extract the receipt data."}],
    )
    ctx = fake_ctx(
        {
            "agent.model": "test-model",
            "backend.completions_url": "http://example.test/v1/chat/completions",
            "summary.incremental_batch_chars": 100000,
        },
        logger=None,
    )

    async def summary_response(**_kwargs):
        return SUMMARY.replace(
            "Keep the results sheet and the exact column order.",
            active_constraint,
        )

    async def patch_response_from_llm(**_kwargs):
        return patch_response

    monkeypatch.setattr(
        thread_summarizer, "arequest_summary_text", summary_response
    )
    monkeypatch.setattr(
        memory_suggestions, "arequest_summary_text", patch_response_from_llm
    )
    await arun_summary_compaction(
        ctx=ctx, state=state, trigger_reason="test", is_cli_mode=False
    )

    sent = state.messages[-1]["content"]
    assert '<technical type="dialog_summary">' in sent
    assert (
        f"<active_constraints>{active_constraint}</active_constraints>" in sent
    ) is in_summary
    assert state.shared.working_memory["constraints"] == expected_memory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "updates"),
    [
        ("I should inspect the input and decide what to do next. " * 3, {}),
        ("cat <<'EOF'\n<goal>shell data</goal>\nEOF", {}),
        ("cat <<EOF\n<state></state>", {}),
        ("bash <<EOF\n<state></state>", {}),
        ("/bin/bash -s <<EOF\n<state></state>", {}),
        ("tee <<EOF\n<state></state>", {}),
        ('printf "%s" <<EOF\n<state></state>', {}),
        ("producer <<-EOF\n<state></state>", {}),
        ("cat <<EOF\n    EOF\n<state></state>\nEOF", {}),
        ("cat <<-EOF\n    EOF\n<state></state>\n\tEOF", {}),
        (
            "cat <<'EOF'\n<state></state>\nEOF\n<goal>Ship reports</goal>",
            {"goal": "Ship reports"},
        ),
        (
            "producer <<EOF\n<state></state>\nEOF\n<goal>Ship reports</goal>",
            {"goal": "Ship reports"},
        ),
        ('The shell prints "<goal>example</goal>".', {}),
        (
            "mask << SHIFT\n<goal>Save the shift result</goal>",
            {"goal": "Save the shift result"},
        ),
        (
            "mask << SHIFT\n<goal>Save the shift result</goal>\n<state>\nSHIFT\n</state>",
            {"goal": "Save the shift result", "state": "SHIFT"},
        ),
        (
            "<state>\nflags = mask << shift\n</state>\n<goal>new</goal>",
            {"state": "flags = mask << shift", "goal": "new"},
        ),
        (
            "<knowledge>\n```bash\ncat <<EOF\n```\n</knowledge>\n<goal>new</goal>\nEOF",
            {"knowledge": "```bash\ncat <<EOF\n```", "goal": "new"},
        ),
        (
            "<goal>new</goal>\n<knowledge>\ncat <<EOF\nAn incomplete sketch.\n</knowledge>",
            {"knowledge": "cat <<EOF\nAn incomplete sketch.", "goal": "new"},
        ),
        (
            "<knowledge>\nCommand fragment:\ncat <<EOF\n</knowledge>\n<state>\nEOF\n</state>",
            {"knowledge": "Command fragment:\ncat <<EOF", "state": "EOF"},
        ),
        (
            "<knowledge>\ncat <<EOF\n</knowledge>\nEOF\n</knowledge><state>Done</state>",
            {"knowledge": "cat <<EOF\n</knowledge>\nEOF", "state": "Done"},
        ),
        (
            "Let me inspect the files.\n</think>\n"
            "<goal>  Index the reports.  \n\n"
            "<constraints>  Keep the  existing files.  \n\n"
            "<state>\n</state>\n\n"
            '<knowledge>\n- The index is called "archive".\n- It has 3 rows.\n'
            "</knowledge>\n\n</summary>\n\n"
            "The user can find the index in the project folder.",
            {
                "goal": "Index the reports.",
                "constraints": "Keep the  existing files.",
                "state": "",
                "knowledge": '- The index is called "archive".\n- It has 3 rows.',
            },
        ),
        (
            "<state>cat <<'EOF'\n<goal>"
            + "command output " * 10
            + "</goal>\n<constraints></constraints>",
            {},
        ),
        (
            '"<state>'
            + "quoted example " * 10
            + '\n<knowledge>example only</knowledge>"',
            {},
        ),
        (
            'printf "%s" "\n</think>\n<goal>demo\n<state></state>\n'
            '</summary>\n"',
            {},
        ),
        (
            "Here is an example from the manual.\n</think>\n"
            "<goal>demo\n<state></state>",
            {},
        ),
        (
            "<goal>demo\n<state></state>\n</summary>\nDo not use this example.",
            {},
        ),
        (
            "<goal>demo\n<state>\n<unknown>unfinished\n</state>",
            {},
        ),
        (
            '<goal>demo\n<state>\n<constraints src="plan">Keep originals\n</state>',
            {},
        ),
        (
            "<goal>demo\n<state>\n<next-step>unfinished\n</state>",
            {},
        ),
        (
            "<goal>Save the receipts.\n<state>\nStatus: <next-step>unfinished\n</state>",
            {},
        ),
        (
            "<goal>Save receipts.\n<knowledge><!-- sample\n</knowledge>\n<state></state>",
            {},
        ),
        (
            "<goal>Save receipts.\n<knowledge><![CDATA[\n</knowledge>\n<state></state>",
            {},
        ),
        (
            "<goal>Save receipts.\n<knowledge><?draft\n</knowledge>\n<state></state>",
            {},
        ),
        (
            "<!-- sample\n</think>\n<goal>Save receipts.\n<state></state>",
            {},
        ),
        (
            "<![CDATA[\n</think>\n<goal>Save receipts.\n<state></state>",
            {},
        ),
        (
            "<goal>Save receipts.\n<knowledge>Keep `<!--` and `<![CDATA[` literal.</knowledge>",
            {
                "goal": "Save receipts.",
                "knowledge": "Keep `<!--` and `<![CDATA[` literal.",
            },
        ),
        (
            "<goal>Keep the index.\n<state></state>\n"
            '<knowledge>\n`<next-step src="plan">` is a literal.\n</knowledge>',
            {
                "goal": "Keep the index.",
                "state": "",
                "knowledge": '`<next-step src="plan">` is a literal.',
            },
        ),
        (
            "<goal>Keep the index.\n<state></state>\n"
            '<knowledge>\n~~~text\n<next-step src="plan">\n~~~\n</knowledge>',
            {
                "goal": "Keep the index.",
                "state": "",
                "knowledge": '~~~text\n<next-step src="plan">\n~~~',
            },
        ),
        (
            "~~~text\n</think>\n<goal>demo\n<state></state>\n</summary>\n~~~",
            {},
        ),
        (
            "<goal>Save the receipts.\n<state>\n~~~text\n</state>\n</summary>\n~~~",
            {},
        ),
        ("<goal>Save the receipts.\n<state>\n~~~text\n</state>", {}),
        ("<goal>Save the receipts.\n<state>\n```text\n</state>", {}),
        (
            "<goal>Save the receipts.\n<state></state>\n</summary>\n~~~",
            {},
        ),
        (
            "```xml\n<state>"
            + "draft one " * 14
            + "\n<knowledge>first</knowledge>\n```\n"
            "```xml\n<state>draft two\n<knowledge>second</knowledge>\n```",
            {},
        ),
        ("<state>" + "incomplete " * 12 + "\n<goal>cut off", {}),
        ("Updated registers. " * 8 + "\n<state></state>", {"state": ""}),
        (
            "Updated registers. "
            * 8
            + "\n<goal></goal><constraints></constraints>"
            "<state></state><knowledge></knowledge>",
            dict.fromkeys(("goal", "constraints", "state", "knowledge"), ""),
        ),
    ],
    ids=[
        "prose",
        "complete-xml-in-heredoc",
        "unclosed-heredoc-prefix",
        "unclosed-bash-heredoc",
        "unclosed-bash-path-heredoc",
        "unclosed-tee-heredoc",
        "unclosed-printf-heredoc",
        "unclosed-strip-tabs-heredoc",
        "indented-heredoc-label-is-data",
        "strip-tabs-heredoc",
        "heredoc-before-valid-patch",
        "bare-heredoc-terminator-before-valid-patch",
        "complete-xml-in-quotes",
        "bit-shift-before-valid-patch",
        "bit-shift-with-label-in-register",
        "bit-shift-as-register-value",
        "fenced-heredoc-syntax-in-register",
        "unclosed-heredoc-syntax-in-register",
        "heredoc-terminator-in-separate-register",
        "register-delimiter-in-heredoc-body",
        "unclosed-final-draft",
        "heredoc",
        "quotation",
        "quoted-think-delimiter-in-command",
        "example-before-think-delimiter",
        "example-in-postscript",
        "unknown-structural-tag",
        "attribute-in-structural-tag",
        "hyphen-in-structural-tag",
        "hyphen-in-tag-with-caption",
        "xml-comment-before-register-close",
        "xml-cdata-before-register-close",
        "xml-processing-instruction-before-register-close",
        "xml-comment-before-think-boundary",
        "xml-cdata-before-think-boundary",
        "xml-delimiters-in-code-spans",
        "literal-tag-with-attribute-and-hyphen",
        "tilde-fenced-literal-tag",
        "tilde-fenced-think-delimiter",
        "register-close-in-tilde-fence",
        "register-close-in-unclosed-tilde-fence",
        "register-close-in-unclosed-backtick-fence",
        "fence-marker-in-postscript",
        "competing-drafts",
        "truncation",
        "clear-one",
        "clear-all",
    ],
)
async def test_compaction_applies_only_intended_memory_updates(
    monkeypatch, patch, updates
):
    old = WorkingMemorySnapshot(
        goal="Old goal",
        constraints="Old constraint",
        state="Old state",
        knowledge="Old knowledge",
    ).to_dict()
    history = [
        {"role": "system", "content": "System message."},
        {"role": "user", "content": "Prepare the archive."},
        {"role": "assistant", "content": "I opened the index."},
    ]
    state = types.SimpleNamespace(
        default_model="test-model",
        thread_id="memory-repair",
        shared=types.SimpleNamespace(working_memory=old.copy()),
        messages=history.copy(),
    )
    ctx = fake_ctx(
        {"summary.incremental_batch_chars": 100000},
        logger=None,
        auxiliary_llm=types.SimpleNamespace(max_tokens=8192),
    )
    labels = []

    async def completion(*, request_label, **_kwargs):
        labels.append(request_label)
        return {
            "summary_batch_1": SUMMARY,
            "working_memory_patch_replace_after_summary": patch,
            "working_memory_deltas_format_repair": (
                "<goal>Old state</goal><constraints></constraints>"
            ),
        }[request_label]

    monkeypatch.setattr(llm, "arequest_chat_completion_text", completion)
    await arun_summary_compaction(
        ctx=ctx, state=state, trigger_reason="test", is_cli_mode=False
    )

    expected = old | updates
    assert state.shared.working_memory == expected
    assert labels == [
        "summary_batch_1",
        "working_memory_patch_replace_after_summary",
    ]
    assert state.messages[0] == history[0]
    sent = state.messages[-1]["content"]
    assert "I opened the index." in sent
    assert "Keep the results sheet and the exact column order." in sent
    assert (
        render_session_memory_for_summary_message(
            WorkingMemorySnapshot(**expected)
        )
        in sent
    )
